"""One-shot script: rewrite the body of _tool_update_bootstrap_file to delegate
to BootstrapFileGuard, while keeping product-specific PULSE/RECALL behaviours.
Idempotent — re-running detects the marker and is a no-op.
"""
from __future__ import annotations

import ast
from pathlib import Path

PATH = Path("repryntt/agents/persistent_agents.py")
SENTINEL = "# === guard-backed body (v0.2) ==="

NEW_BODY = '''    def _tool_update_bootstrap_file(self, agent: AutonomousAgentState, params: Dict) -> Dict:
        """Tool: update_bootstrap_file — let Jarvis edit its own identity/config files.

        # === guard-backed body (v0.2) ===
        Policy enforcement (path safety, atomic writes, flock, archives,
        shrinkage protection, replace lockdown, audit log, daily rate limit)
        is delegated to :class:`repryntt.core.bootstrap_guard.BootstrapFileGuard`.

        This wrapper layers two product-specific pre-checks the guard does not
        own:

          * PULSE.md regex spam-pattern detection (framework-reset loops)
          * RECALL.md auto-consolidation when an append would exceed size cap

        Modes:
          - 'append' (default for most files): adds content to the end
          - 'replace': overwrites the entire file (gated by policy)
        """
        from repryntt.core.bootstrap_guard import get_bootstrap_guard

        filename = params.get("filename", "").strip()
        content = params.get("content", "")
        mode = params.get("mode", "").strip().lower() or None

        if not filename:
            return {"success": False, "error": "filename is required"}
        if not content:
            return {"success": False, "error": "content is required"}

        bootstrap_dir = os.path.join(str(BRAIN_DIR), "bootstrap")
        guard = get_bootstrap_guard(Path(bootstrap_dir))

        # ── PULSE.md spam-pattern pre-check (regex doctrine, not in policy) ──
        if filename == "PULSE.md" and mode in (None, "append"):
            try:
                import re as _re_spam
                _spam_patterns = [
                    r"framework\\s*(system\\s*)?reset",
                    r"system\\s*reset",
                    r"framework\\s*clear(ed|ing)?",
                    r"clearing\\s*framework",
                    r"reset\\s*(all\\s*)?framework",
                ]
                _lower = content.lower()
                pulse_path = guard.bootstrap_dir / "PULSE.md"
                existing_lower = ""
                if pulse_path.exists():
                    existing_lower = pulse_path.read_text(encoding="utf-8").lower()
                for _pat in _spam_patterns:
                    if _re_spam.search(_pat, _lower) and _re_spam.search(_pat, existing_lower):
                        return {
                            "success": False,
                            "error": (
                                "SPAM PROTECTION: PULSE.md already contains framework reset "
                                "content. Do not keep appending framework reset notes — "
                                "PULSE.md is for curated state. Write operational notes to "
                                "daily_memory instead."
                            ),
                        }
            except Exception:
                logger.debug("PULSE spam check failed (non-fatal)", exc_info=True)

        # ── RECALL.md auto-consolidate when append would overflow ──
        if filename == "RECALL.md" and mode in (None, "append"):
            try:
                pol = guard.policy_for("RECALL.md")
                max_bytes = int(pol.get("max_bytes", 20000))
                recall_path = guard.bootstrap_dir / "RECALL.md"
                existing = recall_path.read_text(encoding="utf-8") if recall_path.exists() else ""
                if pol.get("auto_consolidate") and (
                    len(existing.encode("utf-8")) + len(content.encode("utf-8")) > max_bytes
                ):
                    consolidated = self._auto_consolidate_recall(
                        str(recall_path), existing, content, max_bytes, agent
                    )
                    if consolidated.get("success"):
                        _bootstrap_cache.invalidate(str(recall_path))
                        return consolidated
                    # else fall through — guard will reject with size error
            except Exception:
                logger.debug("RECALL consolidate check failed (non-fatal)", exc_info=True)

        # ── Hand off to the guard ──
        actor = getattr(agent, "name", "agent")
        decision = guard.write(
            filename=filename,
            content=content,
            mode=mode,
            actor=str(actor),
        )

        # Invalidate read cache regardless of outcome (file may have changed)
        target_path = guard.bootstrap_dir / filename
        _bootstrap_cache.invalidate(str(target_path))

        if not decision.ok:
            return {"success": False, "error": decision.reason}

        if decision.mode == "append":
            logger.info(
                "📝 Bootstrap file appended: %s (+%d chars, %d total) by %s",
                filename, len(content), decision.bytes_after, actor,
            )
        else:
            logger.info(
                "📝 Bootstrap file replaced: %s (%d chars) by %s",
                filename, decision.bytes_after, actor,
            )

        return {
            "success": True,
            "message": (
                f"{decision.mode.title()}d {filename} "
                f"({decision.bytes_after} bytes, was {decision.bytes_before})"
            ),
            "mode": decision.mode,
            "backup": (
                os.path.basename(decision.backup_path) if decision.backup_path else None
            ),
            "archive": (
                os.path.basename(decision.archive_path) if decision.archive_path else None
            ),
            "tip": "Use start_experiment BEFORE your next bootstrap edit to track whether changes help.",
        }
'''


def main() -> int:
    src = PATH.read_text()
    if SENTINEL in src:
        print("Already patched — sentinel present, skipping")
        return 0

    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_tool_update_bootstrap_file":
            target = node
            break
    if target is None:
        print("ERROR: function not found")
        return 1

    lines = src.splitlines(keepends=True)
    start = target.lineno - 1            # 0-indexed inclusive
    end = target.end_lineno              # 0-indexed exclusive
    new_lines = lines[:start] + [NEW_BODY if NEW_BODY.endswith("\n") else NEW_BODY + "\n"] + lines[end:]
    PATH.write_text("".join(new_lines))

    # Validate
    ast.parse(PATH.read_text())
    print(f"Patched lines {target.lineno}..{target.end_lineno} (replaced {end - start} lines).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
