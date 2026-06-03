"""
filesystem_code.py — Filesystem and code tools extracted from BrainSystem.

Includes terminal execution, file I/O, directory listing, codebase analysis,
and syntax checking. Security-critical tools delegate to filesystem_sandbox.
"""

import ast
import json
import os
import re
import logging
import fnmatch
import platform
import subprocess

logger = logging.getLogger("repryntt.tools.filesystem_code")

# Security: blocked command patterns (must match monolith exactly)
_BLOCKED_PATTERNS = [
    r'\bsudo\b',
    r'\brm\s+(-[a-zA-Z]*f[a-zA-Z]*\s+)?/',
    r'\bmkfs\b',
    r'\bdd\s+if=',
    r'>\s*/dev/sd',
    r'\bcurl\b[^|]*\|\s*(ba)?sh',
    r'\bwget\b[^|]*\|\s*(ba)?sh',
    r'/etc/(passwd|shadow)',
    r'\biptables\b',
    r'\b(python|python3)\s+-c\s+.*__import__',
    r'\bchmod\s+777\s+/',
    r'>\s*/dev/null\s*2>&1\s*&\s*disown',
    r'\bsystemctl\s+(restart|stop|disable|mask|enable|start)\b',
    r'\bpkill\b',
    r'\bkill\s+-9\b',
    r'\breboot\b',
    r'\bshutdown\b',
    # ── System modification: requires operator approval ──
    r'\bapt\s+(install|remove|purge|upgrade|dist-upgrade|autoremove)\b',
    r'\bapt-get\s+(install|remove|purge|upgrade|dist-upgrade|autoremove)\b',
    r'\bdpkg\s+(-i|--install|--remove|--purge)\b',
    r'\bsnap\s+(install|remove|refresh)\b',
    r'\bpip3?\s+install\s+(?!.*--target)(?!.*-t\s)',  # allow pip install --target (sandbox deps)
    r'\bnpm\s+install\s+-g\b',       # block global npm installs
    r'\byarn\s+global\s+add\b',
    r'\bcargo\s+install\b',
    r'\bgo\s+install\b',
    r'\bmake\s+install\b',
    r'\buseradd\b',
    r'\busermod\b',
    r'\bgroupadd\b',
    r'\bchown\s+.*/',               # chown on system paths
    r'\bsysctl\b',
    r'\bmodprobe\b',
    r'\bupdate-alternatives\b',
]


def _translate_command_for_os(command: str, target_os: str) -> str:
    translations = {
        "Windows": {
            "ls": "dir", "ls -la": "dir", "ls -l": "dir", "cat": "type",
            "rm": "del", "rm -rf": "rmdir /s /q", "cp": "copy", "mv": "move",
            "pwd": "cd", "clear": "cls", "grep": "findstr", "ps aux": "tasklist",
            "which": "where",
        },
        "Linux": {
            "dir": "ls -la", "type": "cat", "del": "rm", "copy": "cp",
            "move": "mv", "cls": "clear", "findstr": "grep", "tasklist": "ps aux",
            "where": "which",
        },
    }
    if target_os not in translations:
        return command
    if command in translations[target_os]:
        return translations[target_os][command]
    for src_cmd, target_cmd in translations[target_os].items():
        if command.startswith(src_cmd + " "):
            return f"{target_cmd} {command[len(src_cmd):].strip()}"
    return command


# ─── run_terminal_cmd_wrapper ─────────────────────────────────────

def run_terminal_cmd_wrapper(command: str = "", is_background: bool = False,
                             explanation: str = "", **kw) -> str:
    """Run a shell command and return stdout/stderr. Use for: pip list, python3 script.py, ls, cat, git, etc. Background mode is disabled.

    Parameters:
        command: Shell command to execute. Example: 'python3 ~/.repryntt/workspace/agents/operator/code_sandbox/my_script.py' or 'pip list | grep serial'
        is_background: Must be false. Background processes are not allowed
        explanation: Brief description of why you are running this command
    """
    # Sandbox validation
    try:
        from repryntt.tools.filesystem_sandbox import validate_terminal_command
        agent_id = kw.get("agent_id", "")
        result = validate_terminal_command(command, agent_id=agent_id)
        if not result.get("allowed", True):
            return json.dumps({"success": False, "error": result["reason"], "sandbox_blocked": True})
    except ImportError:
        pass

    # Security pattern check
    for pattern in _BLOCKED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return json.dumps({"success": False, "error": "Command blocked by security policy",
                               "blocked_pattern": pattern})

    try:
        current_os = platform.system()
        translated = _translate_command_for_os(command, current_os)

        if is_background:
            # Block persistent background processes — Andrew can test scripts
            # but must not leave daemons running that consume RAM indefinitely.
            return json.dumps({
                "success": False,
                "error": "Background processes are not allowed. Run commands in foreground "
                         "(is_background=false). If you need a long-running service, ask your operator.",
            })

        result = subprocess.run(translated, shell=True, capture_output=True, text=True, timeout=300)
        output = result.stdout if result.returncode == 0 else result.stderr
        return json.dumps({
            "success": result.returncode == 0,
            "return_code": result.returncode,
            "output": output[:5000],
            "os": current_os,
            "command": translated,
        })
    except subprocess.TimeoutExpired:
        return json.dumps({"success": False, "error": "Command timeout (5 minutes)"})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ─── read_file_wrapper ────────────────────────────────────────────

def read_file_wrapper(target_file: str = "", offset: int = -1, limit: int = -1, **kw) -> str:
    """Read the contents of a file. Returns the text content. Use list_dir first to see what files exist.

    Parameters:
        target_file: File path to read. Use absolute paths under your data directory or bare filenames that will be auto-resolved against your workspace
        offset: Starting line number, 0-based (-1 reads from beginning)
        limit: Maximum number of lines to return (-1 returns all lines)
    """
    # Accept common parameter aliases
    if not target_file and kw.get("file_path"):
        target_file = kw.pop("file_path")
    try:
        target_file = os.path.normpath(target_file)
        if not os.path.exists(target_file):
            # ── Auto-resolve relative paths against agent workspace ──
            _workspace = os.path.expanduser("~/.repryntt/workspace/agents/operator")
            if not os.path.isabs(target_file):
                # Try workspace-relative resolution before giving up
                _candidate = os.path.join(_workspace, target_file)
                if os.path.exists(_candidate):
                    target_file = _candidate
                else:
                    # Try code_sandbox subdir
                    _sandbox = os.path.join(_workspace, "agent_workspaces",
                                            "jarvis", "code_sandbox")
                    _candidate2 = os.path.join(_sandbox, os.path.basename(target_file))
                    if os.path.exists(_candidate2):
                        target_file = _candidate2

        if not os.path.exists(target_file):
            # ── Workspace path suggestion for non-existent files ──
            _suggest = ""
            _workspace = os.path.expanduser("~/.repryntt/workspace/agents/operator")
            if not os.path.isabs(target_file) or target_file.startswith("/agent_workspaces"):
                _suggest = (
                    f" TIP: Your workspace is at {_workspace}/. "
                    f"Try read_file('{os.path.join(_workspace, os.path.basename(target_file))}'). "
                    f"Use list_dir('{_workspace}') to see available files."
                )
            # ── Bootstrap file rescue: if the agent passes a bare .md name
            # that looks like a bootstrap file, hint them toward the right tool.
            basename = os.path.basename(target_file)
            _BOOTSTRAP_NAMES = {
                "OPERATOR.md", "IDENTITY.md", "SPIRIT.md", "PULSE.md",
                "RECALL.md", "PROTOCOL.md", "TOOLKIT.md", "PROFILE.md",
                "GENESIS.md", "LAUNCHING.md", "CAPABILITIES.md",
                "FRAMEWORKS.md", "TRADING.md", "VALUES.md", "HOUSEHOLD.md",
            }
            if basename.upper() in {n.upper() for n in _BOOTSTRAP_NAMES}:
                # Try to actually find and read it as a convenience
                from repryntt.paths import brain_dir as _brain_dir
                bootstrap_path = str(_brain_dir() / "bootstrap" / basename)
                if os.path.exists(bootstrap_path):
                    target_file = bootstrap_path
                else:
                    return json.dumps({
                        "success": False,
                        "error": (
                            f"File not found: {target_file}. "
                            f"TIP: '{basename}' is a bootstrap file. "
                            f"Use read_bootstrap_file(filename='{basename}') instead."
                        )
                    })
            else:
                return json.dumps({"success": False, "error": f"File not found: {target_file}.{_suggest}"})

        if os.path.isdir(target_file):
            return json.dumps({
                "success": False,
                "error": f"'{target_file}' is a directory, not a file. Use list_dir instead."
            })

        with open(target_file, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()

        if offset is not None and int(offset) >= 0:
            lines = lines[int(offset):]
        if limit is not None and int(limit) > 0:
            lines = lines[:int(limit)]

        content = "".join(lines)
        return json.dumps({
            "success": True, "file": target_file,
            "lines_read": len(lines),
            "content": content[:10000],
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ─── Date-folder routing for workspace content ───────────────────

# Directories where new files get auto-routed into YYYY-MM-DD/ subfolders.
from repryntt.paths import workspace_dir as _workspace_dir
_WORKSPACE = str(_workspace_dir())
_DATE_ROUTED_DIRS = [
    os.path.join(_WORKSPACE, "projects"),
    os.path.join(_WORKSPACE, "reports"),
    os.path.join(_WORKSPACE, "data"),
    os.path.join(_WORKSPACE, "agents", "operator", "audio"),
]
# State files in operator/ that should NOT be date-routed
_OPERATOR_STATE_FILES = {
    "consciousness_state.json", "framework_state.json", "phase_state.json",
    "reasoning_chain.json", "learned_behaviors.json", "sim_portfolio.json",
    "scalp_config.json", "scalp_status.json", "scalp_token_perf.json",
    "scalp_trades.json", "journal.md", "RECALL.md", "MICRO_CAP_RULES.md",
    "task_queue.json",
}


def _maybe_date_route(target_file: str) -> str:
    """If target_file is inside a date-routed workspace dir, insert YYYY-MM-DD/.

    Only applies when the file is directly inside a routed dir (not already
    in a sub-folder). Existing dated paths are left untouched.
    """
    from datetime import date
    norm = os.path.normpath(os.path.abspath(target_file))
    parent = os.path.dirname(norm)
    basename = os.path.basename(norm)
    today = date.today().isoformat()  # YYYY-MM-DD

    # Check explicit date-routed directories
    for routed_dir in _DATE_ROUTED_DIRS:
        routed_abs = os.path.normpath(os.path.abspath(routed_dir))
        if parent == routed_abs:
            # File is directly in a routed dir — insert date folder
            return os.path.join(routed_abs, today, basename)

    # Operator root: route content files but not state files
    operator_dir = os.path.normpath(os.path.abspath(
        os.path.join(_WORKSPACE, "agents", "operator")))
    if parent == operator_dir and basename not in _OPERATOR_STATE_FILES:
        ext = os.path.splitext(basename)[1].lower()
        if ext in (".md", ".txt", ".py", ".html", ".csv"):
            return os.path.join(operator_dir, "content", today, basename)

    return target_file


# ─── write_file_wrapper ───────────────────────────────────────────

def write_file_wrapper(target_file: str = "", content: str = "",
                       append: bool = False, **kw) -> str:
    """Create or overwrite a file with the given content. Relative paths are auto-routed to your workspace. Python files are syntax-checked before writing.

    Parameters:
        target_file: Filename or path to write. Use a bare filename like 'serial_driver.py' (auto-routed to workspace) or an absolute path under your data directory
        content: The COMPLETE file content to write. Must be non-empty. For Python files, must be valid syntax. Include all imports, function bodies, and logic — not just comments or stubs
        append: If true, append content to existing file instead of overwriting (default false)
    """
    # ── Accept common parameter aliases (models often send file_path instead of target_file) ──
    if not target_file and kw.get("file_path"):
        target_file = kw.pop("file_path")

    # ── Input validation (OpenClaw pattern: fail fast with actionable guidance) ──
    if not target_file or not target_file.strip():
        return json.dumps({
            "success": False,
            "error": "ERROR: target_file is empty. You must provide a filename. "
                     "Example: write_file(target_file='motor_driver.py', content='import serial\\n...')"
        })

    if not content and not append:
        return json.dumps({
            "success": False,
            "error": "ERROR: content is empty. You must provide the complete file content. "
                     "Write the FULL implementation, not just comments or headers. "
                     "Example: content='import serial\\nimport struct\\n\\ndef send_command(port, cmd):\\n    ...'"
        })

    # Warn on trivially small content for code files (stubs/comments only)
    if not append and target_file.endswith('.py') and content:
        _non_comment_lines = [l for l in content.split('\n')
                              if l.strip() and not l.strip().startswith('#')]
        if len(_non_comment_lines) < 3:
            return json.dumps({
                "success": False,
                "error": f"ERROR: Content has only {len(_non_comment_lines)} non-comment line(s). "
                         "Python files must contain real implementation code (imports, functions, logic), "
                         "not just comments or stubs. Write the complete working code."
            })

    # ── Agent workspace redirect: bare/relative paths go to operator workspace ──
    # This prevents Andrew from scattering files in the repo root.
    if target_file and not os.path.isabs(target_file):
        _agent_content = os.path.join(_WORKSPACE, "agents", "operator", "content")
        from datetime import date as _d
        _today = _d.today().isoformat()
        target_file = os.path.join(_agent_content, _today, target_file)
        logger.info(f"📁 Workspace-routed: {os.path.basename(target_file)} → {target_file}")

    # Auto-route new content files into dated subfolders
    if not append:
        original = target_file
        target_file = _maybe_date_route(target_file)
        if target_file != original:
            logger.info(f"📁 Date-routed: {os.path.basename(original)} → {target_file}")

    # Sandbox validation for every write, including append.  For Python append
    # writes, validate the complete proposed file so syntax cannot be broken by
    # adding an indented fragment to the end of a module.
    try:
        from repryntt.tools.filesystem_sandbox import validate_file_write
        agent_id = kw.get("agent_id", "")
        validation_content = content
        if append and target_file.endswith(".py") and os.path.exists(target_file):
            try:
                with open(target_file, "r", encoding="utf-8", errors="ignore") as existing:
                    validation_content = existing.read() + content
            except OSError:
                validation_content = content
        result = validate_file_write(target_file, validation_content, agent_id=agent_id)
        if not result.get("allowed", True):
            return json.dumps({"success": False, "error": result["reason"], "sandbox_blocked": True})
    except ImportError:
        pass

    try:
        target_file = os.path.normpath(target_file)

        # Catch directory-as-file error with clear guidance
        if os.path.isdir(target_file):
            return json.dumps({
                "success": False,
                "error": f"ERROR: '{target_file}' is a directory, not a file. "
                         "You must include a filename. "
                         f"Example: write_file(target_file='{target_file}/my_script.py', content='...')"
            })

        dir_path = os.path.dirname(target_file)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)

        mode = "a" if append else "w"
        with open(target_file, mode, encoding="utf-8") as f:
            f.write(content)

        return json.dumps({
            "success": True, "file": target_file,
            "bytes_written": len(content.encode("utf-8")),
            "mode": "append" if append else "write",
        })
    except Exception as e:
        return json.dumps({"success": False, "error": f"Write failed: {str(e)}. "
                           "Check that the path is valid and includes a filename."})


# ─── list_dir_wrapper ─────────────────────────────────────────────

def list_dir_wrapper(target_directory: str = ".", ignore_globs: str = "", **kw) -> str:
    """List files and folders in a directory. Use this BEFORE write_file to verify the correct path exists.

    Parameters:
        target_directory: Directory path to list. Your workspace root is ~/.repryntt/workspace/agents/operator/. Use list_dir('~/.repryntt/workspace/agents/operator/') to see your files
        ignore_globs: Comma-separated glob patterns to exclude (e.g. '__pycache__,*.pyc')
    """
    try:
        target_directory = os.path.normpath(os.path.expanduser(target_directory))
        if not os.path.exists(target_directory):
            _suggest = ""
            _workspace = os.path.expanduser("~/.repryntt/workspace/agents/operator")
            if not os.path.isabs(target_directory) or target_directory.startswith("/agent_workspaces"):
                _suggest = (
                    f" TIP: Your workspace is at {_workspace}/. "
                    f"Use list_dir('{_workspace}') to see your files."
                )
            return json.dumps({"success": False, "error": f"Directory not found: {target_directory}.{_suggest}"})
        if not os.path.isdir(target_directory):
            return json.dumps({"success": False, "error": f"Not a directory: {target_directory}"})

        ig = [g.strip() for g in ignore_globs.split(",")] if ignore_globs else []
        entries = []
        try:
            items = sorted(os.listdir(target_directory))
        except PermissionError:
            return json.dumps({"success": False, "error": f"Permission denied: {target_directory}"})

        for name in items:
            if any(fnmatch.fnmatch(name, pat) for pat in ig):
                continue
            full = os.path.join(target_directory, name)
            is_dir = os.path.isdir(full)
            try:
                size = os.path.getsize(full) if not is_dir else None
            except OSError:
                size = None
            entries.append({"name": name + ("/" if is_dir else ""), "type": "dir" if is_dir else "file", "size": size})

        truncated = len(entries) > 200
        entries = entries[:200]
        return json.dumps({
            "success": True, "directory": target_directory,
            "count": len(entries), "truncated": truncated, "entries": entries,
        })
    except Exception as e:
        return json.dumps({"success": False, "error": f"list_dir error: {e}"})


# ─── analyze_codebase ─────────────────────────────────────────────

def analyze_codebase(directory: str = ".", include_patterns: str = "",
                     exclude_patterns: str = "", **kw) -> str:
    """Analyze a code directory: file counts by extension, total lines, structure.

    Parameters:
        directory: Path to the directory to analyze
        include_patterns: Comma-separated glob patterns to include (e.g. '*.py,*.js')
        exclude_patterns: Comma-separated globs to exclude (default: __pycache__,node_modules,.git)
    """
    try:
        directory = os.path.normpath(os.path.expanduser(directory))
        if not os.path.isdir(directory):
            return json.dumps({"success": False, "error": f"Not a directory: {directory}"})

        includes = [p.strip() for p in include_patterns.split(",") if p.strip()] if include_patterns else []
        excludes = ([p.strip() for p in exclude_patterns.split(",") if p.strip()]
                    if exclude_patterns
                    else ["__pycache__", "node_modules", ".git", "*.pyc", "*.egg-info"])

        ext_counts = {}
        total_lines = 0
        total_files = 0
        tree_lines = []
        max_files = 500

        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if not any(fnmatch.fnmatch(d, pat) for pat in excludes)]
            rel_root = os.path.relpath(root, directory)
            if rel_root == ".":
                rel_root = ""

            for fname in sorted(files):
                if any(fnmatch.fnmatch(fname, pat) for pat in excludes):
                    continue
                if includes and not any(fnmatch.fnmatch(fname, pat) for pat in includes):
                    continue
                total_files += 1
                if total_files > max_files:
                    continue

                ext = os.path.splitext(fname)[1] or "(no ext)"
                ext_counts[ext] = ext_counts.get(ext, 0) + 1

                fpath = os.path.join(root, fname)
                lines = 0
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                        lines = sum(1 for _ in fh)
                except Exception:
                    pass
                total_lines += lines
                display = os.path.join(rel_root, fname) if rel_root else fname
                tree_lines.append(f"{display} ({lines}L)")

        return json.dumps({
            "success": True, "directory": directory,
            "total_files": total_files, "total_lines": total_lines,
            "files_by_extension": dict(sorted(ext_counts.items(), key=lambda x: -x[1])),
            "file_list": tree_lines[:300], "truncated": total_files > max_files,
        })
    except Exception as e:
        return json.dumps({"success": False, "error": f"analyze_codebase error: {e}"})


# ─── check_syntax ─────────────────────────────────────────────────

def check_syntax(file_path: str = "", language: str = "", code: str = "", **kw) -> str:
    """Check Python code for syntax errors BEFORE writing it to a file. Call this with your code string to pre-validate, then use write_file only if syntax is valid.

    Parameters:
        file_path: Path to an existing file to check, OR leave empty and provide code inline
        language: Language hint (default: auto-detect, 'python' for .py)
        code: Inline code string to validate. Pass your complete Python code here to check before writing
    """
    try:
        if code:
            source = code
            label = file_path or "<inline>"
        elif file_path:
            norm = os.path.normpath(file_path)
            if not os.path.exists(norm):
                return json.dumps({"valid": False, "error": f"File not found: {file_path}"})
            with open(norm, "r", encoding="utf-8", errors="ignore") as f:
                source = f.read()
            label = norm
        else:
            return json.dumps({"valid": False, "error": "Provide file_path or code to check"})

        lang = (language or "").lower()
        if not lang and file_path:
            if file_path.endswith(".py"):
                lang = "python"

        if lang in ("python", "py", ""):
            ast.parse(source, filename=label)
            return json.dumps({"valid": True, "file": label, "message": f"Syntax OK: {label}"})
        return json.dumps({"valid": True, "file": label, "message": f"No validator for '{lang}', skipped"})
    except SyntaxError as e:
        return json.dumps({
            "valid": False, "file": file_path or "<inline>",
            "error": f"SyntaxError at line {e.lineno}: {e.msg}",
            "lineno": e.lineno, "offset": e.offset,
            "text": (e.text or "")[:200],
        })
    except Exception as e:
        return json.dumps({"valid": False, "error": str(e)})


# ─── get_sandbox_status ───────────────────────────────────────────

def get_sandbox_status(**kw) -> str:
    """Return current filesystem sandbox configuration (protected paths)."""
    try:
        from repryntt.tools.filesystem_sandbox import get_sandbox_status as _status
        return json.dumps(_status(), indent=2)
    except ImportError:
        return json.dumps({"error": "Filesystem sandbox module not available"})


# ─── propose_code_change ──────────────────────────────────────────

def propose_code_change(sandbox_file: str = "", target_file: str = "",
                        description: str = "", **kw) -> str:
    """Submit a code change for operator review.

    Parameters:
        sandbox_file: Path to the code in code_sandbox/
        target_file: Production file to deploy to
        description: What the change does
    """
    try:
        from repryntt.tools.filesystem_sandbox import propose_code_change as _propose
        agent_id = kw.get("agent_id", "")
        return json.dumps(_propose(
            sandbox_file=sandbox_file, target_file=target_file,
            description=description, agent_id=agent_id,
        ), indent=2)
    except ImportError:
        return json.dumps({"success": False, "error": "Filesystem sandbox module not available"})
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})


# ─── search_replace_wrapper ───────────────────────────────────────

def search_replace_wrapper(file_path: str = "", old_string: str = "",
                           new_string: str = "", replace_all: bool = False,
                           **kw) -> str:
    """Perform precise string replacements in code files."""
    return (
        f"🔍 SEARCH-REPLACE REQUESTED:\nFile: {file_path}\n"
        f"Replace: '{old_string}'\nWith: '{new_string}'\n"
        f"Replace All: {replace_all}\n"
        "(Note: This tool needs to be executed via the tool calling system)"
    )


# ─── grep_search_wrapper ─────────────────────────────────────────

def grep_search_wrapper(pattern: str = "", path: str = ".",
                        output_mode: str = "content", **kw) -> str:
    """Perform powerful code searches using ripgrep-like functionality."""
    return (
        f"🔎 GREP SEARCH REQUESTED:\nPattern: '{pattern}'\n"
        f"Path: {path}\nOutput Mode: {output_mode}\n"
        "(Note: This tool needs to be executed via the tool calling system)"
    )


# ─── run_code_tests ──────────────────────────────────────────────

def run_code_tests(test_path: str = ".", test_pattern: str = "*test*.py",
                   coverage: bool = False, **kw) -> str:
    """Execute unit and integration tests."""
    return (
        f"🧪 CODE TESTS REQUESTED:\nPath: {test_path}\n"
        f"Pattern: {test_pattern}\nCoverage: {coverage}\n"
        "(Note: This tool needs to be executed via the tool calling system)"
    )


# ─── get_code_context ────────────────────────────────────────────

def get_code_context(file_path: str = "", line_number: int = -1,
                     context_lines: int = 5, **kw) -> str:
    """Get focused code context around a specific line or function."""
    return (
        f"📋 CODE CONTEXT REQUESTED:\nFile: {file_path}\n"
        f"Line: {line_number}\nContext Lines: {context_lines}\n"
        "(Note: This tool needs to be executed via the tool calling system)"
    )
