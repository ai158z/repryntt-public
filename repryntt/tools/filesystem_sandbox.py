"""
SAIGE Filesystem Sandbox — Protects critical directories and files from
accidental or autonomous deletion/corruption by AI agents.

Created after Jarvis autonomously deleted the entire robot_economy/ directory
(42GB) during disk cleanup on 2026-02-28, taking down the blockchain system.
Extended 2026-03-08 to block production .py file corruption after Andrew
mangled trading_bot/ai72_andahalf.py during a heartbeat.

== HOW IT WORKS ==
1. Every terminal command passes through validate_terminal_command() BEFORE exec.
2. Every file write passes through validate_file_write() BEFORE writing.
3. Destructive operations (rm -rf, rm -r, rmdir, mv, shutil.rmtree, etc.)
   targeting PROTECTED PATHS are BLOCKED and logged.
4. Production .py file writes are BLOCKED — Andrew must use the code sandbox.
5. Any .py file write (outside production dirs) is syntax-validated before allow.
6. Agents can still create/modify non-.py FILES inside protected dirs.

== CODE SANDBOX WORKFLOW ==
Andrew CANNOT directly write to production .py files. Instead:
  1. Write code to agent_workspaces/jarvis/code_sandbox/
  2. Use check_syntax to validate
  3. Use run_terminal_cmd to test (python3 sandbox/file.py)
  4. Use propose_code_change to submit for operator review
  5. Operator deploys approved changes manually

== ADDING/REMOVING PROTECTED PATHS ==
Edit PROTECTED_DIRS / PRODUCTION_CODE_DIRS below.
Only a human (operator) should modify this file.
"""

import os
import re
import json
import logging
from pathlib import Path
from datetime import datetime, timezone

logger = logging.getLogger("filesystem_sandbox")

# ──────────────────────────────────────────────────────────────────────
# Project source root (auto-detected: parent of tools/)
# ──────────────────────────────────────────────────────────────────────
SAIGE_ROOT = str(Path(__file__).resolve().parent.parent)
REPO_ROOT = str(Path(__file__).resolve().parents[2])

ROOT_CODE_WRITE_CONFIG_KEY = "allow_autonomous_root_code_writes"
ROOT_CODE_WRITE_ENV = "REPRYNTT_ALLOW_AUTONOMOUS_ROOT_CODE_WRITES"

# Data directory root (~/.repryntt/) — agent state lives here
from repryntt.paths import get_data_dir as _get_data_dir, operator_dir as _operator_dir, logs_dir as _logs_dir
DATA_ROOT = str(_get_data_dir())

# ──────────────────────────────────────────────────────────────────────
# PROTECTED DIRECTORIES — cannot be deleted, moved, or renamed by agents.
# These are relative to SAIGE_ROOT.  Agents CAN still read/write/create
# individual files inside them — only recursive deletion of the entire
# directory is blocked.
# ──────────────────────────────────────────────────────────────────────
PROTECTED_DIRS = {
    # Core system
    "brain",
    "brain/bootstrap",
    "brain/knowledge_base",
    "config",
    "scripts",
    "src",
    "nervous_system",
    
    # Agent state
    "agents",
    "agents/persistent_agents.py",
    
    # Data / blockchain
    "robot_economy",
    "robot_economy_data",
    "blockchain_explorer",
    "wallets",
    "database",
    "data",
    
    # Logs
    "logs",
    
    # Infrastructure
    "models",
    "backups",
    "certs",
    "k8s",
    "monitoring",
    
    # Web / UI
    "saige_web",
    "saige_ui",
    "templates",
    "web",
    
    # AI subsystems
    "ai_social_network",
    "consciousness_hierarchy_implementation",
    "swarm_storage",
    "vision",
    "robotics",
    
    # External deps
    "jetsonMCP",
    "jetson-containers",
    "external",
}

# ──────────────────────────────────────────────────────────────────────
# PROTECTED FILES — cannot be overwritten with empty/tiny content.
# Agents CAN edit these (search_replace), but cannot truncate them to
# near-zero length.
# ──────────────────────────────────────────────────────────────────────
PROTECTED_FILES = {
    # Bootstrap — Jarvis's core identity and operational files
    "brain/bootstrap/SPIRIT.md",
    "brain/bootstrap/PROFILE.md",
    "brain/bootstrap/PULSE.md",
    "brain/bootstrap/DRIVES.md",
    "brain/bootstrap/GOALS.md",
    # Core system files
    "persistent_agents.py",
    "brain/brain_system.py",
    "brain/filesystem_sandbox.py",   # Protect myself!
    "brain/gmail_integration.py",
    "brain/consciousness_nervous_system.py",
    "jarvis_consciousness.py",
    "deploy.py",
    "docker-compose.yml",
    "Dockerfile",
    "contract_state.json",
    "node2040_brain.json",
    "brainfile2.json",
    "start_saige_production.sh",
}

# Minimum file sizes (bytes) — if a write would make a protected file
# smaller than this, it's blocked.  Prevents accidental truncation.
PROTECTED_FILE_MIN_BYTES = 100

# ──────────────────────────────────────────────────────────────────────
# PRODUCTION CODE DIRECTORIES — .py file writes are COMPLETELY BLOCKED.
# Andrew must use the code sandbox instead.  These are relative to
# SAIGE_ROOT.  Non-.py files (configs, data, logs) are still writable.
# ──────────────────────────────────────────────────────────────────────
PRODUCTION_CODE_DIRS = {
    "brain",
    "brain/bootstrap",
    "brain/knowledge_base",
    "trading_bot",
    "scripts",
    "src",
    "nervous_system",
    "robotics",
    "vision",
    "saige_web",
    "monitoring",
}

# Also block root-level .py files (persistent_agents.py, deploy.py, etc.)
BLOCK_ROOT_PY_WRITES = True

# ──────────────────────────────────────────────────────────────────────
# CODE SANDBOX — safe directory where Andrew can write/test code freely
# ──────────────────────────────────────────────────────────────────────
CODE_SANDBOX_DIR = str(_operator_dir() / "code_sandbox")
CODE_PROPOSALS_DIR = str(_operator_dir() / "code_proposals")

# ──────────────────────────────────────────────────────────────────────
# SANDBOX LOG — all blocked operations are recorded here
# ──────────────────────────────────────────────────────────────────────
SANDBOX_LOG_PATH = str(_logs_dir() / "sandbox_blocks.log")

def _log_block(action: str, target: str, command: str = "", agent_id: str = ""):
    """Log a blocked operation to both the logger and the sandbox log file."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = (
        f"[{ts}] BLOCKED | agent={agent_id or 'unknown'} | "
        f"action={action} | target={target} | command={command[:200]}"
    )
    logger.warning(f"🛡️ SANDBOX: {entry}")
    
    try:
        os.makedirs(os.path.dirname(SANDBOX_LOG_PATH), exist_ok=True)
        with open(SANDBOX_LOG_PATH, "a") as f:
            f.write(entry + "\n")
    except Exception:
        pass  # Don't crash if logging fails


def _resolve_path(raw_path: str) -> str:
    """Resolve a path to an absolute path, expanding ~ and vars."""
    return os.path.realpath(os.path.expanduser(os.path.expandvars(raw_path.strip().strip("'\"") )))


def _resolve_path_from(raw_path: str, cwd: str | None = None) -> str:
    """Resolve raw_path, using cwd for relative paths."""
    raw = os.path.expanduser(os.path.expandvars(raw_path.strip().strip("'\"")))
    if not os.path.isabs(raw) and cwd:
        raw = os.path.join(cwd, raw)
    return os.path.realpath(raw)


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _load_root_code_write_toggle() -> bool:
    """Return whether autonomous writes into the checked-out project are allowed."""
    if ROOT_CODE_WRITE_ENV in os.environ:
        return _truthy(os.environ.get(ROOT_CODE_WRITE_ENV))

    config_paths = [
        Path(DATA_ROOT) / "brain" / "ai_config.json",
        Path(REPO_ROOT) / "config" / "ai_config.json",
    ]
    for cfg_path in config_paths:
        try:
            if not cfg_path.exists():
                continue
            raw = json.loads(cfg_path.read_text())
            if ROOT_CODE_WRITE_CONFIG_KEY in raw:
                return _truthy(raw.get(ROOT_CODE_WRITE_CONFIG_KEY))
            ai_provider = raw.get("ai_provider")
            if isinstance(ai_provider, dict) and ROOT_CODE_WRITE_CONFIG_KEY in ai_provider:
                return _truthy(ai_provider.get(ROOT_CODE_WRITE_CONFIG_KEY))
            security = raw.get("security")
            if isinstance(security, dict) and ROOT_CODE_WRITE_CONFIG_KEY in security:
                return _truthy(security.get(ROOT_CODE_WRITE_CONFIG_KEY))
        except Exception:
            continue
    return False


def root_code_writes_enabled() -> bool:
    """Public helper used by status/UI code."""
    return _load_root_code_write_toggle()


def _is_in_repo(abs_path: str) -> bool:
    repo = REPO_ROOT.rstrip("/")
    abs_path = os.path.realpath(abs_path).rstrip("/")
    return abs_path == repo or abs_path.startswith(repo + "/")


def _is_repo_write_protected(abs_path: str) -> bool:
    """True when an autonomous write targets the checked-out project tree."""
    if root_code_writes_enabled():
        return False
    if not _is_in_repo(abs_path):
        return False
    if _is_in_sandbox(abs_path):
        return False
    return True


def _root_code_block_reason(target_path: str, abs_path: str) -> str:
    return (
        f"BLOCKED: Autonomous root-code writes are disabled. "
        f"File: {target_path} ({abs_path})\n\n"
        f"This checked-out project is protected from Andrew/Jarvis direct edits. "
        f"Write code to {CODE_SANDBOX_DIR}/ and submit it with propose_code_change. "
        f"To deliberately allow self-improving root-code edits, set "
        f"'{ROOT_CODE_WRITE_CONFIG_KEY}': true in ai_config.json or set "
        f"{ROOT_CODE_WRITE_ENV}=1, then restart/reload the daemon."
    )


def _ignore_terminal_write_target(target: str) -> bool:
    """Return true for shell bookkeeping targets that are not real project files."""
    clean = target.strip().strip("'\"")
    return (
        not clean
        or clean.startswith("&")
        or clean.startswith("/dev/")
        or clean in {"-", "/dev/null"}
    )


def _is_protected_dir(abs_path: str) -> bool:
    """Check if abs_path IS or IS AN ANCESTOR of any protected directory."""
    abs_path = abs_path.rstrip("/")
    saige = SAIGE_ROOT.rstrip("/")
    
    for pdir in PROTECTED_DIRS:
        protected_abs = os.path.join(saige, pdir).rstrip("/")
        # Block if:
        # 1) Target IS the protected directory itself
        # 2) Target is a parent of the protected directory.
        if abs_path == protected_abs or protected_abs.startswith(abs_path + "/"):
            return True

    # Also protect key data directories under ~/.repryntt/
    data_root = DATA_ROOT.rstrip("/")
    data_protected = {
        "brain/bootstrap", "brain/skills", "brain/skill_packages",
        "workspace/agents/operator", "wallet",
    }
    for dpdir in data_protected:
        dp_abs = os.path.join(data_root, dpdir).rstrip("/")
        if abs_path == dp_abs or dp_abs.startswith(abs_path + "/"):
            return True

    return False


def _is_protected_file(abs_path: str) -> bool:
    """Check if abs_path matches a protected file."""
    saige = SAIGE_ROOT.rstrip("/")
    for pfile in PROTECTED_FILES:
        if abs_path == os.path.join(saige, pfile):
            return True
    return False


def _is_in_production_code_dir(abs_path: str) -> bool:
    """Check if abs_path is inside any PRODUCTION_CODE_DIRS."""
    saige = SAIGE_ROOT.rstrip("/")
    for pdir in PRODUCTION_CODE_DIRS:
        prod_abs = os.path.join(saige, pdir).rstrip("/")
        if abs_path.startswith(prod_abs + "/") or abs_path == prod_abs:
            return True
    return False


def _is_root_level_py(abs_path: str) -> bool:
    """Check if abs_path is a .py file directly in SAIGE_ROOT (not a subdirectory)."""
    saige = SAIGE_ROOT.rstrip("/")
    parent = os.path.dirname(abs_path).rstrip("/")
    return parent == saige and abs_path.endswith(".py")


def _is_in_sandbox(abs_path: str) -> bool:
    """Check if abs_path is inside the code sandbox directory."""
    sandbox_abs = os.path.join(SAIGE_ROOT, CODE_SANDBOX_DIR).rstrip("/")
    return abs_path.startswith(sandbox_abs + "/") or abs_path == sandbox_abs


def validate_python_syntax(content: str, filename: str = "<unknown>") -> dict:
    """
    Validate Python source code syntax using ast.parse().
    Returns {"valid": True} or {"valid": False, "error": "..."}.
    """
    import ast
    try:
        ast.parse(content, filename=filename)
        return {"valid": True}
    except SyntaxError as e:
        return {
            "valid": False,
            "error": f"SyntaxError at line {e.lineno}: {e.msg}",
            "lineno": e.lineno,
            "text": (e.text or "")[:200],
        }


# ──────────────────────────────────────────────────────────────────────
# COMMAND PATTERNS that could delete/move/destroy directories
# ──────────────────────────────────────────────────────────────────────
# These regex patterns extract target paths from destructive commands.
DESTRUCTIVE_PATTERNS = [
    # rm -rf, rm -r, rm --recursive
    (r'\brm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*\s+)*(.+)', "rm"),
    # rmdir
    (r'\brmdir\s+(?:--[a-z-]+\s+)*(.+)', "rmdir"),
    # mv (source is being moved — could destroy dir structure)
    (r'\bmv\s+(?:-[a-zA-Z]+\s+)*(.+?)\s+\S+\s*$', "mv"),
    # shutil.rmtree in python -c
    (r'shutil\.rmtree\s*\(\s*["\']([^"\']+)["\']', "rmtree"),
    # find ... -delete  or  find ... -exec rm
    (r'\bfind\s+(\S+)\s+.*(?:-delete|-exec\s+rm)', "find-delete"),
]


def validate_terminal_command(command: str, agent_id: str = "") -> dict:
    """
    Validate a terminal command BEFORE execution.
    
    Returns:
        {"allowed": True}                         — command is safe
        {"allowed": False, "reason": "..."}       — command is BLOCKED
    """
    if not command or not command.strip():
        return {"allowed": True}
    
    # Normalize: handle chained commands (&&, ||, ;, |)
    # Split on command separators and check each sub-command
    sub_commands = re.split(r'\s*(?:&&|\|\||;)\s*', command)
    
    cwd_hint = None
    for sub_cmd in sub_commands:
        sub_cmd = sub_cmd.strip()
        if not sub_cmd:
            continue

        cd_match = re.match(r'^cd\s+([^&|;]+)$', sub_cmd)
        if cd_match:
            cwd_hint = _resolve_path(cd_match.group(1))
            continue
        
        for pattern, cmd_type in DESTRUCTIVE_PATTERNS:
            match = re.search(pattern, sub_cmd, re.IGNORECASE)
            if not match:
                continue
            
            # Extract the target path(s)
            raw_targets = match.group(1).strip()
            
            # Split on spaces (respecting quoted strings)
            try:
                import shlex
                targets = shlex.split(raw_targets)
            except ValueError:
                targets = raw_targets.split()
            
            for target in targets:
                # Skip flags
                if target.startswith("-"):
                    continue
                
                abs_target = _resolve_path(target)
                
                # For 'rm' specifically: only block recursive deletion of dirs
                # Allow 'rm file.txt' inside protected dirs (single file ops)
                if cmd_type == "rm":
                    # Check if -r or -R flag is present
                    has_recursive = bool(re.search(r'\brm\s+(-[a-zA-Z]*[rR]|--recursive)', sub_cmd))
                    if not has_recursive:
                        # Non-recursive rm — only block if target IS a protected dir
                        if os.path.isdir(abs_target) and _is_protected_dir(abs_target):
                            reason = (
                                f"BLOCKED: Cannot delete protected directory '{target}' "
                                f"(resolves to {abs_target}). "
                                f"This directory is in the PROTECTED_DIRS list in "
                                f"brain/filesystem_sandbox.py. "
                                f"Only the operator (Nate) can remove protected directories."
                            )
                            _log_block(f"rm-dir", abs_target, command, agent_id)
                            return {"allowed": False, "reason": reason}
                        continue  # Allow rm of individual files
                    
                # Recursive rm, rmdir, mv, find-delete, rmtree
                if _is_protected_dir(abs_target):
                    reason = (
                        f"BLOCKED: Cannot {cmd_type} protected path '{target}' "
                        f"(resolves to {abs_target}). "
                        f"This path is in the PROTECTED_DIRS list in "
                        f"brain/filesystem_sandbox.py. "
                        f"Only the operator (Nate) can remove or move protected directories. "
                        f"If you need to free disk space, delete SPECIFIC FILES within "
                        f"subdirectories instead of removing entire directory trees."
                    )
                    _log_block(cmd_type, abs_target, command, agent_id)
                    return {"allowed": False, "reason": reason}
    
    # ── Block terminal commands that write to production .py files ──
    # Catches: echo/cat/tee > file.py, cp/mv source.py brain/target.py,
    # sed -i ... brain/file.py, python -c "open('brain/file.py','w')..."
    TERMINAL_WRITE_PATTERNS = [
        # Shell redirections:  > file  or  >> file
        (r'(?<!\d)>{1,2}\s*([^\s]+)', "redirect"),
        # tee output to file
        (r'\btee\s+(?:-[a-zA-Z]+\s+)*([^\s]+)\b', "tee"),
        # cp ... target  (last arg is the target)
        (r'\bcp\s+(?:-[a-zA-Z]+\s+)*\S+\s+([^\s]+)\s*$', "cp"),
        # mv ... target
        (r'\bmv\s+(?:-[a-zA-Z]+\s+)*\S+\s+([^\s]+)\s*$', "mv"),
        # sed -i (in-place edit)
        (r'\bsed\s+(?:-[a-zA-Z]*i[a-zA-Z]*\s+).*?([^\s]+)\s*$', "sed-i"),
        # python/python3 writing to a file via open()
        (r'(?:python3?)\s+.*open\s*\(\s*["\']([^"\']+)["\']', "python-write"),
    ]

    cwd_hint = None
    for sub_cmd in sub_commands:
        sub_cmd = sub_cmd.strip()
        if not sub_cmd:
            continue
        cd_match = re.match(r'^cd\s+([^&|;]+)$', sub_cmd)
        if cd_match:
            cwd_hint = _resolve_path(cd_match.group(1))
            continue
        for pattern, write_type in TERMINAL_WRITE_PATTERNS:
            match = re.search(pattern, sub_cmd, re.IGNORECASE | re.DOTALL)
            if not match:
                continue
            target_py = match.group(1).strip()
            if _ignore_terminal_write_target(target_py):
                continue
            abs_target = _resolve_path_from(target_py, cwd_hint)
            if _is_repo_write_protected(abs_target):
                reason = _root_code_block_reason(target_py, abs_target)
                _log_block(f"terminal-{write_type}-repo", abs_target, command, agent_id)
                return {"allowed": False, "reason": reason}
            if (not root_code_writes_enabled() and
                    (_is_in_production_code_dir(abs_target) or
                     (BLOCK_ROOT_PY_WRITES and _is_root_level_py(abs_target)))):
                reason = (
                    f"BLOCKED: Cannot write to production Python file via terminal. "
                    f"File: {target_py} ({write_type})\n\n"
                    f"Production .py files are protected. "
                    f"Write to {CODE_SANDBOX_DIR}/ instead, "
                    f"then use propose_code_change to submit for operator review."
                )
                _log_block(f"terminal-{write_type}-py", abs_target, command, agent_id)
                return {"allowed": False, "reason": reason}

        # Python heredocs/scripts often use relative paths after a prior cd.
        for match in re.finditer(
            r'open\s*\(\s*["\']([^"\']+)["\']\s*,\s*["\'][^"\']*[wax+][^"\']*["\']',
            sub_cmd,
            re.IGNORECASE | re.DOTALL,
        ):
            target = match.group(1).strip()
            if _ignore_terminal_write_target(target):
                continue
            abs_target = _resolve_path_from(target, cwd_hint)
            if _is_repo_write_protected(abs_target):
                reason = _root_code_block_reason(target, abs_target)
                _log_block("terminal-python-open-repo", abs_target, command, agent_id)
                return {"allowed": False, "reason": reason}

    return {"allowed": True}


def _auto_backup_file(abs_path: str, agent_id: str = ""):
    """
    Create an automatic backup of a protected file BEFORE any write.
    Backup is saved as <filename>.autobackup (overwritten each time).
    This ensures we can always recover the last known good version.
    """
    if not os.path.exists(abs_path):
        return
    try:
        backup_path = abs_path + ".autobackup"
        import shutil
        shutil.copy2(abs_path, backup_path)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        logger.info(
            f"🛡️ SANDBOX: Auto-backup created for protected file: "
            f"{os.path.basename(abs_path)} -> {os.path.basename(backup_path)} "
            f"(agent={agent_id or 'unknown'}, time={ts})"
        )
    except Exception as e:
        logger.error(f"🛡️ SANDBOX: Failed to create auto-backup for {abs_path}: {e}")


def validate_file_write(target_path: str, content: str, agent_id: str = "") -> dict:
    """
    Validate a file write operation BEFORE execution.
    
    Three layers of protection:
    1. BLOCK writes to .py files in PRODUCTION_CODE_DIRS (and root .py files)
    2. SYNTAX-VALIDATE any .py file write that IS allowed (e.g. sandbox dir)
    3. Prevent truncation of PROTECTED_FILES (existing behavior)
    
    Returns:
        {"allowed": True}
        {"allowed": False, "reason": "..."}
    """
    abs_path = _resolve_path(target_path)
    is_python = abs_path.endswith(".py")

    # ── Layer 0: Block all direct writes into the checked-out project tree ──
    if _is_repo_write_protected(abs_path):
        reason = _root_code_block_reason(target_path, abs_path)
        _log_block("write-repo-root", abs_path, f"write {len(content or '')}B", agent_id)
        return {"allowed": False, "reason": reason}

    # ── Layer 1: Block .py writes to production code directories ──
    if is_python and not _is_in_sandbox(abs_path) and not root_code_writes_enabled():
        # Check production code directories
        if _is_in_production_code_dir(abs_path):
            sandbox_path = os.path.join(SAIGE_ROOT, CODE_SANDBOX_DIR)
            reason = (
                f"BLOCKED: Cannot write Python file to production directory. "
                f"File: {target_path}\n\n"
                f"Production .py files are protected from direct modification. "
                f"To modify code safely:\n"
                f"  1. Write your code to {CODE_SANDBOX_DIR}/ instead\n"
                f"  2. Use check_syntax to validate it\n"
                f"  3. Test it with run_terminal_cmd: python3 {CODE_SANDBOX_DIR}/your_file.py\n"
                f"  4. Use propose_code_change to submit for operator review\n\n"
                f"Only the operator (Nate) can deploy code to production directories."
            )
            _log_block("write-production-py", abs_path, f"write {len(content)}B .py", agent_id)
            return {"allowed": False, "reason": reason}

        # Check root-level .py files
        if BLOCK_ROOT_PY_WRITES and _is_root_level_py(abs_path):
            reason = (
                f"BLOCKED: Cannot write Python file to project root. "
                f"File: {target_path}\n\n"
                f"Root-level .py files are protected. "
                f"Write to {CODE_SANDBOX_DIR}/ instead, "
                f"then use propose_code_change to submit for operator review."
            )
            _log_block("write-root-py", abs_path, f"write {len(content)}B .py", agent_id)
            return {"allowed": False, "reason": reason}

    # ── Layer 2: Syntax-validate any allowed .py write ──
    if is_python and content and content.strip():
        syntax_result = validate_python_syntax(content, filename=abs_path)
        if not syntax_result.get("valid", True):
            reason = (
                f"BLOCKED: Python syntax error in proposed write to {target_path}\n"
                f"{syntax_result['error']}\n\n"
                f"Fix the syntax error and try again. "
                f"Use check_syntax to validate your code before writing."
            )
            _log_block("syntax-error-py", abs_path, syntax_result["error"][:200], agent_id)
            return {"allowed": False, "reason": reason}

    # ── Layer 3: Protected file truncation check (existing) ──
    if _is_protected_file(abs_path):
        content_bytes = len(content.encode('utf-8')) if content else 0

        if content_bytes < PROTECTED_FILE_MIN_BYTES:
            try:
                current_size = os.path.getsize(abs_path)
            except OSError:
                current_size = 0

            if current_size > PROTECTED_FILE_MIN_BYTES and content_bytes < PROTECTED_FILE_MIN_BYTES:
                reason = (
                    f"BLOCKED: Cannot truncate protected file '{target_path}' "
                    f"(current: {current_size} bytes → proposed: {content_bytes} bytes). "
                    f"Protected files cannot be reduced below {PROTECTED_FILE_MIN_BYTES} bytes. "
                    f"Use search_replace for targeted edits instead of rewriting the entire file."
                )
                _log_block("truncate-file", abs_path, f"write {content_bytes}B", agent_id)
                return {"allowed": False, "reason": reason}

        # Auto-backup before allowing the write
        _auto_backup_file(abs_path, agent_id)

    return {"allowed": True}


def get_sandbox_status() -> dict:
    """Return current sandbox configuration for agents to inspect."""
    return {
        "repo_root": REPO_ROOT,
        "saige_root": SAIGE_ROOT,
        ROOT_CODE_WRITE_CONFIG_KEY: root_code_writes_enabled(),
        "root_code_write_env": ROOT_CODE_WRITE_ENV,
        "protected_dirs": sorted(PROTECTED_DIRS),
        "protected_files": sorted(PROTECTED_FILES),
        "protected_file_min_bytes": PROTECTED_FILE_MIN_BYTES,
        "sandbox_log": SANDBOX_LOG_PATH,
        "note": (
            "These paths are protected by the filesystem sandbox. "
            "Agents cannot delete/move/rename protected directories or "
            "truncate protected files. Only the operator can modify this list."
        )
    }


def propose_code_change(sandbox_file: str, target_file: str,
                        description: str = "", agent_id: str = "") -> dict:
    """
    Submit a code change proposal for operator review.
    
    The agent writes working, tested code to the sandbox, then calls this
    to request that the operator deploy it to the production target.
    
    Args:
        sandbox_file: Path to the file in CODE_SANDBOX_DIR (the new code)
        target_file: Production path where the code should be deployed
        description: What this change does and why
        agent_id: Which agent is proposing
    
    Returns:
        {"success": True, "proposal_file": "..."} or
        {"success": False, "error": "..."}
    """
    abs_sandbox = _resolve_path(sandbox_file)
    abs_target = _resolve_path(target_file)

    # Verify sandbox file exists
    if not os.path.exists(abs_sandbox):
        return {"success": False, "error": f"Sandbox file not found: {sandbox_file}"}

    # Verify it's actually in the sandbox
    if not _is_in_sandbox(abs_sandbox):
        return {
            "success": False,
            "error": f"Source file must be in {CODE_SANDBOX_DIR}/, not {sandbox_file}"
        }

    # Read and syntax-validate the sandbox file
    with open(abs_sandbox, "r", encoding="utf-8", errors="ignore") as f:
        code = f.read()

    if abs_sandbox.endswith(".py"):
        syntax = validate_python_syntax(code, filename=abs_sandbox)
        if not syntax.get("valid", True):
            return {
                "success": False,
                "error": f"Syntax error in sandbox file: {syntax['error']}"
            }

    # Create proposal
    ts = datetime.now(timezone.utc)
    proposal_id = ts.strftime("%Y%m%d_%H%M%S") + f"_{agent_id or 'unknown'}"
    proposals_dir = os.path.join(SAIGE_ROOT, CODE_PROPOSALS_DIR)
    os.makedirs(proposals_dir, exist_ok=True)
    proposal_path = os.path.join(proposals_dir, f"{proposal_id}.json")

    proposal = {
        "proposal_id": proposal_id,
        "timestamp": ts.isoformat(),
        "agent_id": agent_id or "unknown",
        "sandbox_file": sandbox_file,
        "target_file": target_file,
        "description": description,
        "code_length_bytes": len(code.encode("utf-8")),
        "code_lines": code.count("\n") + 1,
        "status": "pending_review",
    }

    with open(proposal_path, "w") as f:
        json.dump(proposal, f, indent=2)

    logger.info(
        f"📋 CODE PROPOSAL: {proposal_id} | {sandbox_file} → {target_file} "
        f"| {description[:100]} | agent={agent_id}"
    )

    return {
        "success": True,
        "proposal_file": proposal_path,
        "proposal_id": proposal_id,
        "message": (
            f"✅ Code change proposal submitted (ID: {proposal_id}).\n"
            f"  Source: {sandbox_file}\n"
            f"  Target: {target_file}\n"
            f"  Description: {description}\n\n"
            f"The operator (Nate) will review and deploy if approved. "
            f"Do NOT attempt to copy the file to production yourself."
        )
    }
