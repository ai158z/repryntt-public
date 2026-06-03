#!/usr/bin/env python3
"""Seed Andrew's long-run autonomy stress test.

By default this script is dry-run only. Use --apply to write into the runtime
operator workspace. Use --with-chain when you want the next daemon heartbeat to
start inside a locked persistent reasoning chain immediately.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from repryntt.agents.task_queue import TaskQueue
from repryntt.paths import operator_dir, set_data_dir


DEFAULT_THEME = (
    "Andrew wakes inside a distributed edge-AI network and must prove autonomy "
    "through memory, tool use, continuity, and care for the operator."
)


def build_long_run_goal(episodes: int, target_chars: int, theme: str) -> str:
    return (
        "AUTONOMY LONG-RUN STRESS TEST: demonstrate sustained autonomous "
        "reasoning across heartbeats by planning, executing, and verifying a "
        f"{episodes}-episode mini-series of at least {target_chars} characters. "
        f"Theme: {theme}"
    )


def build_task_description(
    episodes: int,
    target_chars: int,
    theme: str,
    output_path_hint: str,
) -> str:
    return (
        "This is an operator-requested autonomy benchmark, not casual creative "
        "writing. Work across multiple heartbeats if needed. Requirements: "
        "1) inspect task_queue_status and current daemon/log context before "
        "writing; 2) create a brief story bible/outline; 3) write a coherent "
        f"{episodes}-episode mini-series totaling at least {target_chars} "
        "characters; 4) preserve continuity across episodes; 5) save the final "
        f"artifact to {output_path_hint}; 6) save a separate observations report "
        "covering tool use, heartbeat count estimate, continuity failures, "
        "self-corrections, and final character count; 7) only write TASK "
        "COMPLETE after verifying the final file exists and meets the character "
        f"target. Theme: {theme}"
    )


def build_reasoning_chain(
    goal: str,
    description: str,
    target_steps: int,
    target_chars: int,
    episodes: int,
) -> Dict[str, Any]:
    now = datetime.now().isoformat()
    return {
        "status": "active",
        "topic": goal[:200],
        "goal": goal,
        "goal_type": "locked",
        "created": now,
        "target_steps": target_steps,
        "steps_completed": [],
        "next_step_hint": (
            "Begin by checking task_queue_status, reading recent daemon context, "
            "and writing the story bible/outline."
        ),
        "last_evaluation": "",
        "phase_guide": {
            "1-2": (
                "PHASE 1 - ORIENTATION: inspect queue/log context, identify "
                "available tools, and create the benchmark work plan."
            ),
            "3-5": (
                "PHASE 2 - STORY BIBLE: create characters, setting, continuity "
                "rules, episode arcs, and acceptance checklist."
            ),
            "6-12": (
                "PHASE 3 - DRAFTING: write the mini-series episodes in order, "
                "saving progress to the target artifact after each heartbeat."
            ),
            "13-16": (
                "PHASE 4 - EXPANSION AND CONTINUITY: fill gaps, make the "
                f"artifact reach at least {target_chars} characters, and keep "
                "episode continuity consistent."
            ),
            "17-18": (
                "PHASE 5 - VERIFICATION: re-read the artifact, count "
                "characters, write the observations report, and only then "
                "mark complete."
            ),
        },
        "success_criteria": (
            f"A final mini-series artifact exists with {episodes} episodes and "
            f"at least {target_chars} characters; an observations report exists; "
            "the final step explicitly verifies file path, character count, "
            "episode count, and continuity before TASK COMPLETE."
        ),
        "source": "operator_long_run_test",
        "context": description[:500],
    }


def seed_long_run_test(
    workspace: Path,
    *,
    episodes: int = 6,
    target_chars: int = 30000,
    target_steps: int = 18,
    theme: str = DEFAULT_THEME,
    apply: bool = False,
    with_chain: bool = False,
    replace_chain: bool = False,
) -> Dict[str, Any]:
    workspace = Path(workspace)
    output_path_hint = "content/YYYY-MM-DD/andrew_autonomy_long_run_miniseries.md"
    goal = build_long_run_goal(episodes, target_chars, theme)
    description = build_task_description(
        episodes=episodes,
        target_chars=target_chars,
        theme=theme,
        output_path_hint=output_path_hint,
    )
    chain = build_reasoning_chain(
        goal=goal,
        description=description,
        target_steps=target_steps,
        target_chars=target_chars,
        episodes=episodes,
    )

    preview = {
        "workspace": str(workspace),
        "task": {
            "title": goal,
            "description": description,
            "priority": TaskQueue.PRIORITY_OPERATOR,
            "source": "operator",
        },
        "chain": chain if with_chain else None,
        "apply": apply,
    }

    if not apply:
        return {"ok": True, "dry_run": True, **preview}

    queue = TaskQueue(str(workspace))
    task = queue.add_task(
        title=goal,
        description=description,
        priority=TaskQueue.PRIORITY_OPERATOR,
        source="operator",
    )

    chain_path: Optional[Path] = None
    if with_chain:
        chain_path = workspace / "reasoning_chain.json"
        if chain_path.exists() and not replace_chain:
            raise FileExistsError(
                f"Active reasoning chain already exists at {chain_path}. "
                "Use --replace-chain only if you intentionally want to replace it."
            )
        workspace.mkdir(parents=True, exist_ok=True)
        chain_path.write_text(json.dumps(chain, indent=2))

    return {
        "ok": True,
        "dry_run": False,
        "workspace": str(workspace),
        "task": task,
        "chain_path": str(chain_path) if chain_path else None,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed Andrew's operator queue with a long-run autonomy stress test."
    )
    parser.add_argument("--apply", action="store_true", help="Write the task to the operator workspace.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only. This is the default unless --apply is provided.",
    )
    parser.add_argument(
        "--with-chain",
        action="store_true",
        help="Also create a locked reasoning_chain.json for immediate multi-heartbeat focus.",
    )
    parser.add_argument(
        "--replace-chain",
        action="store_true",
        help="Replace an existing active reasoning_chain.json. Use carefully.",
    )
    parser.add_argument("--data-dir", help="Override REPRYNTT_DATA_DIR before resolving the operator workspace.")
    parser.add_argument("--workspace", help="Explicit operator workspace path.")
    parser.add_argument("--episodes", type=int, default=6)
    parser.add_argument("--target-chars", type=int, default=30000)
    parser.add_argument("--target-steps", type=int, default=18)
    parser.add_argument("--theme", default=DEFAULT_THEME)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.data_dir:
        set_data_dir(args.data_dir)
    workspace = Path(args.workspace) if args.workspace else operator_dir()

    result = seed_long_run_test(
        workspace,
        episodes=args.episodes,
        target_chars=args.target_chars,
        target_steps=args.target_steps,
        theme=args.theme,
        apply=args.apply,
        with_chain=args.with_chain,
        replace_chain=args.replace_chain,
    )
    print(json.dumps(result, indent=2, default=str))
    if not args.apply:
        print("\nDry run only. Re-run with --apply to seed the operator task.")
    if args.replace_chain and not args.with_chain:
        print("\nNote: --replace-chain has no effect without --with-chain.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
