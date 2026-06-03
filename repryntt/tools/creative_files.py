"""
creative_files.py — Creative workspace file tools extracted from BrainSystem.

All functions accept a ``brain_path`` (str or Path) pointing to the brain/
directory.  Creative work products are stored in the unified workspace at
``~/.repryntt/workspace/projects/``. Per-chain subdirectories organize
output by project/topic.
"""

import json
import time
import logging
from pathlib import Path

logger = logging.getLogger("repryntt.tools.creative_files")

# ── Unified workspace path ──
# Creative output goes to the shared projects directory so all agents
# and the human can find it in one place.
_WORKSPACE_ROOT = Path.home() / ".repryntt" / "workspace"
_PROJECTS_DIR = _WORKSPACE_ROOT / "projects"


def _creative_dir(brain_path, chain_id: str = "") -> Path:
    # Use unified workspace/projects/ directory instead of brain/creative_workspace/
    d = _PROJECTS_DIR
    if chain_id:
        d = d / chain_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ext(file_type: str) -> str:
    return {"txt": ".txt", "json": ".json", "md": ".md"}.get(file_type, ".txt")


def create_creative_file(brain_path, chain_id: str = "", filename: str = "",
                         file_type: str = "txt", initial_content: str = "", **kw) -> str:
    """Create a new creative writing file for long-form content accumulation.

    Parameters:
        chain_id: The chain ID this file belongs to
        filename: Name of the file (without extension)
        file_type: File type — txt, json, md
        initial_content: Optional initial content to write
    """
    try:
        d = _creative_dir(brain_path, chain_id)
        d.mkdir(parents=True, exist_ok=True)
        fp = d / f"{filename}{_ext(file_type)}"
        with open(fp, "w", encoding="utf-8") as f:
            if file_type == "json":
                json.dump({"content": initial_content, "created_at": time.time(),
                           "chain_id": chain_id}, f, indent=2)
            else:
                f.write(initial_content)
        return f"Created creative file: {fp.name}"
    except Exception as e:
        logger.error(f"Error creating creative file: {e}")
        return f"Failed to create creative file: {e}"


def write_to_creative_file(brain_path, chain_id: str = "", filename: str = "",
                           content: str = "", file_type: str = "txt", **kw) -> str:
    """Write/overwrite content to a creative writing file.

    Parameters:
        chain_id: The chain ID this file belongs to
        filename: Name of the file (without extension)
        content: Content to write
        file_type: File type — txt, json, md
    """
    try:
        d = _creative_dir(brain_path, chain_id)
        fp = d / f"{filename}{_ext(file_type)}"
        with open(fp, "w", encoding="utf-8") as f:
            if file_type == "json":
                json.dump({"content": content, "updated_at": time.time(),
                           "chain_id": chain_id}, f, indent=2)
            else:
                f.write(content)
        return f"Wrote {len(content)} characters to {filename}{_ext(file_type)}"
    except Exception as e:
        logger.error(f"Error writing to creative file: {e}")
        return f"Failed to write to creative file: {e}"


def append_to_creative_file(brain_path, chain_id: str = "", filename: str = "",
                            content: str = "", file_type: str = "txt", **kw) -> str:
    """Append content to an existing creative writing file.

    Parameters:
        chain_id: The chain ID this file belongs to
        filename: Name of the file (without extension)
        content: Content to append
        file_type: File type — txt, json, md
    """
    try:
        d = _creative_dir(brain_path, chain_id)
        fp = d / f"{filename}{_ext(file_type)}"
        if not fp.exists():
            return create_creative_file(brain_path, chain_id, filename, file_type, content)

        if file_type == "json":
            try:
                with open(fp, "r") as rf:
                    data = json.load(rf)
                if "content" in data:
                    data["content"] += content
                    data["updated_at"] = time.time()
                with open(fp, "w") as wf:
                    json.dump(data, wf, indent=2)
            except (json.JSONDecodeError, FileNotFoundError):
                data = {"content": content, "updated_at": time.time(), "chain_id": chain_id}
                with open(fp, "w") as wf:
                    json.dump(data, wf, indent=2)
        else:
            with open(fp, "a", encoding="utf-8") as f:
                f.write(content)

        return f"Appended {len(content)} characters to {filename}{_ext(file_type)}"
    except Exception as e:
        logger.error(f"Error appending to creative file: {e}")
        return f"Failed to append to creative file: {e}"


def read_creative_file(brain_path, chain_id: str = "", filename: str = "",
                       file_type: str = "txt", max_chars: int = 5000, **kw) -> str:
    """Read content from a creative writing file.

    Parameters:
        chain_id: The chain ID this file belongs to
        filename: Name of the file (without extension)
        file_type: File type — txt, json, md
        max_chars: Maximum characters to return (for context window management)
    """
    try:
        d = _creative_dir(brain_path, chain_id)
        fp = d / f"{filename}{_ext(file_type)}"
        if not fp.exists():
            return f"File not found: {filename}{_ext(file_type)}"

        with open(fp, "r", encoding="utf-8") as f:
            if file_type == "json":
                data = json.load(f)
                content = data.get("content", "")
            else:
                content = f.read()

        max_chars = int(max_chars)
        if len(content) > max_chars:
            return f"[TRUNCATED - showing last {max_chars} chars]\n{content[-max_chars:]}"
        return content
    except Exception as e:
        logger.error(f"Error reading creative file: {e}")
        return f"Failed to read creative file: {e}"


def get_creative_workspace_status(brain_path, chain_id: str = "", **kw) -> dict:
    """Get status of creative workspace files.

    Parameters:
        chain_id: Optional specific chain ID to check
    """
    try:
        base = _creative_dir(brain_path)
        if not base.exists():
            return {"status": "no_workspace", "message": "Creative workspace not yet created"}

        status = {"chains": {}}
        dirs = [base / chain_id] if chain_id else [d for d in base.iterdir() if d.is_dir()]

        for chain_dir in dirs:
            if not chain_dir.exists():
                status["chains"][chain_dir.name] = {"file_count": 0, "files": [], "total_size": 0}
                continue
            files = list(chain_dir.glob("*"))
            status["chains"][chain_dir.name] = {
                "file_count": len(files),
                "files": [f.name for f in files],
                "total_size": sum(f.stat().st_size for f in files),
            }
        return status
    except Exception as e:
        logger.error(f"Error getting creative workspace status: {e}")
        return {"error": str(e)}
