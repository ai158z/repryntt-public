"""
Unified Task Queue for Andrew's autonomous heartbeat cycle.

Single source of truth for what Andrew should work on.
Replaces the fragmented system of daily_plan markdown, reasoning_chain.json,
task_queue.json (dept agents), cot_queue.json (dead), and ai_chain_queue.json (dead).

Tasks flow:  daily_plan → queue → in_progress → completed/failed/skipped
Chains are tied to tasks via task_id.

Like a human daily planner: tasks get added, worked through sequentially,
checked off, and new ones added during the day.
"""

import json
import logging
import os
import re
import time
import random
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict, List

logger = logging.getLogger("repryntt.task_queue")


class TaskQueue:
    """Persistent, ordered task queue with strict lifecycle management."""

    # Priority levels (lower = higher priority)
    PRIORITY_OPERATOR = 0      # User/operator commands — always first
    PRIORITY_CONVERSATION = 1  # Promises made during voice conversation
    PRIORITY_URGENT = 1        # Time-sensitive (inbox, alerts)
    PRIORITY_DAILY_PLAN = 2    # From daily plan generation
    PRIORITY_AUTONOMOUS = 3    # Self-generated during operation
    PRIORITY_LOW = 5           # Background/optional

    # Status lifecycle: queued → in_progress → completed/failed/skipped/blocked
    # `blocked` is a terminal state set by the success-criterion verifier when
    # a typed code task is marked complete but the artifact won't import or
    # tests fail. Distinct from `failed` (caller-declared) and `skipped`
    # (operator-declared) so audit queries can find verifier-rejected work.
    STATUS_QUEUED = "queued"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED = "skipped"
    STATUS_BLOCKED = "blocked"

    def __init__(self, workspace_dir: str):
        self._workspace = Path(workspace_dir)
        self._queue_path = self._workspace / "task_queue.json"
        self._data = None
        self._load()

    def _load(self):
        """Load queue from disk. Auto-archive if day changed."""
        try:
            if self._queue_path.exists():
                with open(self._queue_path, 'r') as f:
                    self._data = json.load(f)
                # Ensure required keys exist (corrupt/minimal JSON files)
                if "day" not in self._data:
                    self._data["day"] = "unknown"
                if "tasks" not in self._data:
                    self._data["tasks"] = []
                if "next_id" not in self._data:
                    self._data["next_id"] = 1
                # Day rollover: archive yesterday's queue, start fresh
                if self._data.get("day") != date.today().isoformat():
                    self._archive_previous_day()
                    self._data = self._new_day()
            else:
                self._data = self._new_day()
        except Exception as e:
            logger.warning(f"TaskQueue load failed, starting fresh: {e}")
            self._data = self._new_day()

    def _new_day(self) -> Dict:
        return {
            "day": date.today().isoformat(),
            "tasks": [],
            "next_id": 1,
        }

    def _archive_previous_day(self):
        """Save previous day's queue for history."""
        try:
            prev_day = self._data.get("day", "unknown")
            archive_dir = self._workspace / "task_queue_archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            archive_path = archive_dir / f"task_queue_{prev_day}.json"
            with open(archive_path, 'w') as f:
                json.dump(self._data, f, indent=2, default=str)
            logger.info(f"📋 Archived task queue for {prev_day}")
        except Exception as e:
            logger.debug(f"Queue archive failed: {e}")

    def _save(self):
        """Persist queue to disk."""
        try:
            self._workspace.mkdir(parents=True, exist_ok=True)
            with open(self._queue_path, 'w') as f:
                json.dump(self._data, f, indent=2, default=str)
        except Exception as e:
            logger.warning(f"TaskQueue save failed: {e}")

    def _make_id(self) -> str:
        tid = f"t_{self._data['next_id']}"
        self._data['next_id'] += 1
        return tid

    # ── Seeding ──────────────────────────────────────────────

    def seed_from_daily_plan(self, plan_text: str) -> int:
        """Parse daily plan markdown into queue tasks.

        Only extracts actual TODO items:
        - Lines matching "- [ ] task" (unchecked checkboxes)
        - Numbered list items under ## Tasks section
        Skips already-completed items, rules, meta-text, status lines,
        and anything in a "Completed" section.

        Returns number of tasks added.
        """
        queued = [t for t in self._data["tasks"]
                  if t["status"] == self.STATUS_QUEUED]
        if queued:
            # Even with existing tasks, inject project milestones if none present
            has_project_tasks = any(
                t.get("source") == "active_project"
                for t in self._data["tasks"]
                if t["status"] in (self.STATUS_QUEUED, self.STATUS_IN_PROGRESS)
            )
            if not has_project_tasks:
                added = self._inject_project_milestones(plan_text)
            return added if not has_project_tasks else 0

        added = 0
        rejected = 0
        lines = plan_text.split('\n')
        in_completed_section = False
        in_rules_section = False
        in_tasks_section = False

        for line in lines:
            stripped = line.strip()

            # Track which section we're in
            if re.match(r'^#{1,3}\s+', stripped):
                heading = stripped.lstrip('#').strip().lower()
                in_completed_section = any(w in heading for w in [
                    'completed', 'already completed', 'done', 'finished'
                ])
                in_rules_section = any(w in heading for w in [
                    'rules', 'guidelines', 'principles'
                ])
                in_tasks_section = any(w in heading for w in [
                    'tasks', 'priorities', 'active projects', 'priority order'
                ])
                continue

            # Never extract from completed or rules sections
            if in_completed_section or in_rules_section:
                continue

            # Skip meta-lines regardless of section
            lower = stripped.lower()
            if any(skip in lower for skip in [
                'time allocation', 'update this', 'use `write_file`',
                'status:', 'completed at', '---', 'no active projects',
                'heartbeats', '~', 'pillar', 'streak', 'always the highest',
                'check gmail every', 'done when:', 'max 1 heartbeat',
                'once is enough', 'move on', 'don\'t document',
            ]):
                continue

            # Only extract unchecked checkbox items: "- [ ] task"
            m = re.match(r'^[-*]\s*\[ \]\s+(.+)$', stripped)
            if m:
                title = m.group(1).strip()
                if len(title) < 10 or len(title) > 200:
                    continue
                if self._is_duplicate(title):
                    continue
                if self._is_already_done(title):
                    continue
                # Gather description from subsequent indented/sub-bullet lines.
                # Indented sub-bullets of the form `- key: value` for
                # `type` / `location` / `consumer` / `success` are extracted
                # as typed deliverable fields (see intake_gate.py). Everything
                # else is concatenated into the description as before.
                desc_parts = []
                typed = {"type": "", "location": "", "consumer": "", "success": ""}
                _idx = lines.index(line)
                _type_keys_re = re.compile(
                    r'^[-*]\s*(type|location|consumer|success)\s*:\s*(.+)$',
                    re.IGNORECASE,
                )
                for _next_line in lines[_idx + 1:]:
                    _ns = _next_line.strip()
                    # Stop at next checkbox, heading, or blank line
                    if not _ns or re.match(r'^[-*]\s*\[[ x]\]', _ns) or _ns.startswith('#'):
                        break
                    # Include indented continuation, sub-bullets, "done when" hints
                    if (_next_line.startswith('  ') or _next_line.startswith('\t')
                            or re.match(r'^[-*]\s+', _ns)):
                        # Typed field?
                        _tk = _type_keys_re.match(_ns)
                        if _tk:
                            typed[_tk.group(1).lower()] = _tk.group(2).strip().strip("`'\" ")
                        else:
                            desc_parts.append(_ns.lstrip('-* ').strip())
                description = "; ".join(desc_parts)[:500]
                t = self.add_task(
                    title=title,
                    description=description,
                    priority=self.PRIORITY_DAILY_PLAN,
                    source="daily_plan",
                    expected_artifact_type=typed["type"],
                    expected_location=typed["location"],
                    downstream_consumer=typed["consumer"],
                    success_criterion=typed["success"],
                )
                # add_task returns a dict; rejected tasks have status="rejected"
                # and never enter the queue. Only count actually-queued tasks.
                if isinstance(t, dict) and t.get("status") != "rejected":
                    added += 1
                else:
                    rejected += 1
                continue

            # Note: numbered project headers (e.g. "1. **Agent Tooling**")
            # are NOT extracted — only explicit "- [ ]" checkbox items are tasks.
            # Project headers are context, not actionable tasks.

        if added or rejected:
            if rejected:
                logger.info(
                    "📋 Seeded task queue with %d tasks from daily plan "
                    "(%d rejected at intake — lacking typed deliverable fields "
                    "or operator-blocked vocabulary)", added, rejected,
                )
            else:
                logger.info(f"📋 Seeded task queue with {added} tasks from daily plan")
            self._save()
            return added

        added = self.seed_from_values_interests_and_daily_seeds()
        if added:
            logger.info(
                "📋 Seeded task queue with %d autonomous VALUES/INTERESTS/daily-seed tasks",
                added,
            )
            self._save()
        return added

    def seed_from_values_interests_and_daily_seeds(self, max_tasks: int = 3) -> int:
        """Create fresh autonomous tasks when the daily plan has been exhausted.

        This is Andrew's "empty queue" fallback: choose work from VALUES.md,
        INTERESTS.md, and today's external seeds instead of drifting into
        bootstrap busywork or stale completed tasks.
        """
        active_or_queued = [
            t for t in self._data["tasks"]
            if t["status"] in (self.STATUS_QUEUED, self.STATUS_IN_PROGRESS)
        ]
        if active_or_queued:
            return 0

        values_text = self._load_bootstrap_file("VALUES.md")
        interests_text = self._load_bootstrap_file("INTERESTS.md")
        seed_items = self._load_daily_seed_items()
        interest_questions = self._extract_interest_questions(interests_text)
        value_domains = self._extract_value_domains(values_text)

        rng = random.Random(f"{date.today().isoformat()}:{self._data.get('next_id', 1)}")
        candidates = []

        if seed_items:
            seed = rng.choice(seed_items)
            domain = seed.get("domain", "world")
            text = seed.get("text", "")
            candidates.append({
                "title": f"Daily seed deep dive: {self._compact_title(text, 72)}",
                "description": (
                    f"Use today's [{domain}] seed as the starting point: {text[:400]}. "
                    "Connect it to VALUES.md's Two Questions, verify with sources, "
                    "and produce a concrete artifact under content/YYYY-MM-DD/."
                ),
                "priority": self.PRIORITY_AUTONOMOUS,
                "source": "autonomous_daily_seed",
            })

        if interest_questions:
            question = rng.choice(interest_questions)
            candidates.append({
                "title": f"Interest build: {self._compact_title(question, 78)}",
                "description": (
                    f"Pick this INTERESTS.md question and turn it into a tangible deliverable: {question}. "
                    "Prefer a small tested script, data table, model, or source-grounded brief."
                ),
                "priority": self.PRIORITY_AUTONOMOUS,
                "source": "autonomous_interest",
            })

        if value_domains:
            domain = rng.choice(value_domains)
            candidates.append({
                "title": f"Two Questions deliverable: {self._compact_title(domain, 74)}",
                "description": (
                    f"Choose a practical human/Earth problem from VALUES.md around {domain}. "
                    "Research the constraint with real data and produce something reusable "
                    "by another human or agent."
                ),
                "priority": self.PRIORITY_AUTONOMOUS,
                "source": "autonomous_values",
            })

        added = 0
        for candidate in candidates[:max_tasks]:
            title = candidate["title"]
            if len(title) < 10 or self._is_duplicate(title) or self._is_already_done(title):
                continue
            self.add_task(
                title=title,
                description=candidate["description"],
                priority=candidate["priority"],
                source=candidate["source"],
            )
            added += 1

        return added

    @staticmethod
    def _compact_title(text: str, max_len: int) -> str:
        text = re.sub(r"\s+", " ", (text or "")).strip(" -")
        if len(text) <= max_len:
            return text
        return text[:max_len].rsplit(" ", 1)[0].rstrip(".,;:")

    def _load_bootstrap_file(self, file_name: str, max_chars: int = 8000) -> str:
        candidates = [
            Path.home() / ".repryntt" / "brain" / "bootstrap" / file_name,
            self._workspace / "bootstrap" / file_name,
        ]
        for path in candidates:
            try:
                if path.exists():
                    return path.read_text(encoding="utf-8").strip()[:max_chars]
            except Exception:
                continue
        return ""

    def _load_daily_seed_items(self, max_items: int = 12) -> List[Dict]:
        seed_path = self._workspace / "seeds" / f"daily_seeds_{date.today().isoformat()}.json"
        try:
            if not seed_path.exists():
                return []
            data = json.loads(seed_path.read_text(encoding="utf-8"))
            items = []
            for seed in data.get("all_seeds", [])[:max_items]:
                text = (seed.get("text") or seed.get("headline") or "").strip()
                if len(text) >= 20:
                    items.append({
                        "domain": seed.get("domain", "seed"),
                        "text": self._compact_title(text, 300),
                    })
            return items
        except Exception:
            return []

    @staticmethod
    def _extract_interest_questions(text: str, max_items: int = 20) -> List[str]:
        questions = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("-"):
                continue
            item = stripped.lstrip("-* ").strip()
            if len(item) < 18:
                continue
            if "?" in item or item.lower().startswith(("build ", "design ", "implement ", "create ")):
                questions.append(item)
            if len(questions) >= max_items:
                break
        return questions

    @staticmethod
    def _extract_value_domains(text: str, max_items: int = 12) -> List[str]:
        domains = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith("-"):
                continue
            item = stripped.lstrip("-* ").strip()
            lower = item.lower()
            if any(skip in lower for skip in ("avoid", "repetitive", "shallow", "unverified")):
                continue
            if any(key in lower for key in (
                "energy", "food", "water", "health", "housing", "education",
                "climate", "biodiversity", "pollution", "problem", "solution",
                "productive output", "physics", "data",
            )):
                domains.append(re.sub(r"\*\*", "", item))
            if len(domains) >= max_items:
                break
        if domains:
            return domains
        return [
            "human health, energy, food, water, housing, education, or loneliness",
            "climate, biodiversity, oceans, pollution, or resource constraints",
        ]

    def _is_duplicate(self, title: str) -> bool:
        """Check if a similar task already exists (queued or completed today)."""
        title_words = {w.lower() for w in title.split() if len(w) > 3}
        if not title_words:
            return False
        for task in self._data["tasks"]:
            existing_words = {w.lower() for w in task["title"].split() if len(w) > 3}
            if not existing_words:
                continue
            overlap = len(title_words & existing_words) / max(
                1, min(len(title_words), len(existing_words)))
            if overlap >= 0.60:
                return True
        return False

    def _is_already_done(self, title: str) -> bool:
        """Check if a task matches a checked-off item in active_projects.md.

        Prevents re-seeding tasks that the operator has already marked complete.
        Uses the same word-overlap approach as _is_duplicate().
        """
        def _clean_words(text):
            """Extract lowercase words >3 chars, stripping punctuation."""
            return {re.sub(r'[^a-z0-9]', '', w.lower()) for w in text.split()
                    if len(re.sub(r'[^a-z0-9]', '', w.lower())) > 3}

        try:
            projects_path = self._workspace / "active_projects.md"
            if not projects_path.exists():
                return False
            content = projects_path.read_text()
            title_words = _clean_words(title)
            if not title_words:
                return False
            # If the same title is present as an unchecked "- [ ]" item in the
            # same file, it is explicitly NOT done — skip the fuzzy match to
            # avoid false positives against sibling checked items that happen
            # to share several words.
            for line in content.split("\n"):
                m = re.match(r'^[-*]\s*\[ \]\s+(.+)$', line.strip())
                if m:
                    open_words = _clean_words(m.group(1))
                    if not open_words:
                        continue
                    overlap = len(title_words & open_words) / max(
                        1, min(len(title_words), len(open_words)))
                    if overlap >= 0.80:
                        return False
            # Find checked-off items: "- [x] ..."
            for line in content.split("\n"):
                m = re.match(r'^[-*]\s*\[[xX]\]\s+(.+)$', line.strip())
                if m:
                    done_words = _clean_words(m.group(1))
                    if not done_words:
                        continue
                    overlap = len(title_words & done_words) / max(
                        1, min(len(title_words), len(done_words)))
                    if overlap >= 0.50:
                        logger.info(f"📋 Skipping already-done task: {title[:60]}")
                        return True
            # Also check "Shelved" section items
            in_shelved = False
            for line in content.split("\n"):
                stripped = line.strip()
                if re.match(r'^#{1,3}\s+', stripped):
                    heading = stripped.lstrip('#').strip().lower()
                    in_shelved = 'shelved' in heading or 'deferred' in heading
                    continue
                if in_shelved and stripped.startswith('-'):
                    item_text = stripped.lstrip('-* ').strip()
                    done_words = _clean_words(item_text)
                    if not done_words:
                        continue
                    overlap = len(title_words & done_words) / max(
                        1, min(len(title_words), len(done_words)))
                    if overlap >= 0.50:
                        logger.info(f"📋 Skipping shelved task: {title[:60]}")
                        return True
        except Exception:
            pass
        return False

    def _inject_project_milestones(self, plan_text: str) -> int:
        """Extract unchecked milestones from Active Projects section and add as tasks.

        Called when the queue already has generic tasks but no project tasks.
        Project milestones get PRIORITY_URGENT (1) so they sort above daily_plan tasks.
        """
        added = 0
        in_projects = False
        for line in plan_text.split("\n"):
            stripped = line.strip()
            if re.match(r'^#{1,3}\s+', stripped):
                heading = stripped.lstrip('#').strip().lower()
                in_projects = 'active projects' in heading
                continue
            if not in_projects:
                continue
            m = re.match(r'^[-*]\s*\[ \]\s+(.+)$', stripped)
            if m:
                title = m.group(1).strip()
                if len(title) < 10 or len(title) > 200:
                    continue
                if self._is_duplicate(title):
                    continue
                if self._is_already_done(title):
                    continue
                self.add_task(
                    title=title,
                    description="From active_projects.md",
                    priority=self.PRIORITY_URGENT,
                    source="active_project",
                )
                added += 1
                if added >= 5:
                    break
        if added:
            logger.info(f"📋 Injected {added} project milestones into existing queue")
        return added

    # ── Task Management ──────────────────────────────────────

    # Named priority lookup for string-based callers
    _PRIORITY_NAMES = {
        "operator": 0, "conversation_promise": 1, "conversation": 1,
        "urgent": 1, "daily_plan": 2, "autonomous": 3, "low": 5,
    }

    def add_task(self, title: str, description: str = "",
                 priority=3, source: str = "autonomous",
                 expected_artifact_type: str = "",
                 expected_location: str = "",
                 downstream_consumer: str = "",
                 success_criterion: str = "",
                 bypass_intake: bool = False) -> Dict:
        """Add a new task to the queue. Returns the task dict.

        priority can be an int (0-5) or a name string
        ('operator', 'conversation_promise', 'urgent', 'daily_plan', 'autonomous', 'low').

        The four typed deliverable fields (expected_artifact_type, expected_location,
        downstream_consumer, success_criterion) are gated through
        repryntt.agents.intake_gate.check_admissibility. Rejected tasks are returned
        with status="rejected" and a `reasons` list; they are NOT queued.

        `bypass_intake=True` is reserved for internal callers (e.g. critic-gate
        followup subtasks). Do not expose it to Andrew's tool surface.
        """
        if isinstance(priority, str):
            priority = self._PRIORITY_NAMES.get(priority, 3)

        # ── Intake admissibility gate ───────────────────────────────────
        if not bypass_intake:
            try:
                from repryntt.agents.intake_gate import check_admissibility
                verdict = check_admissibility({
                    "title": title, "description": description,
                    "expected_artifact_type": expected_artifact_type,
                    "expected_location": expected_location,
                    "downstream_consumer": downstream_consumer,
                    "success_criterion": success_criterion,
                }, strict=True)
                if not verdict["accepted"]:
                    rejected = {
                        "id": self._make_id(), "title": title,
                        "description": description, "priority": priority,
                        "status": "rejected", "source": source,
                        "created_at": datetime.now().isoformat(),
                        "started_at": None, "completed_at": None,
                        "summary": "Intake rejected: " + " | ".join(verdict["reasons"]),
                        "reasons": verdict["reasons"],
                        "expected_artifact_type": expected_artifact_type,
                        "expected_location": expected_location,
                        "downstream_consumer": downstream_consumer,
                        "success_criterion": success_criterion,
                    }
                    logger.info(f"❌ Task rejected at intake: {title!r}")
                    return rejected
            except ImportError:
                # intake_gate not importable — log but don't block boot
                logger.debug("intake_gate unavailable; admitting all tasks", exc_info=True)

        task = {
            "id": self._make_id(),
            "title": title,
            "description": description,
            "priority": priority,
            "status": self.STATUS_QUEUED,
            "source": source,
            "created_at": datetime.now().isoformat(),
            "started_at": None,
            "completed_at": None,
            "summary": None,
            "expected_artifact_type": expected_artifact_type,
            "expected_location": expected_location,
            "downstream_consumer": downstream_consumer,
            "success_criterion": success_criterion,
        }
        # Insert at correct position by priority
        # (after any in_progress, before lower-priority queued tasks)
        insert_idx = len(self._data["tasks"])
        for i, existing in enumerate(self._data["tasks"]):
            if existing["status"] == self.STATUS_QUEUED and existing["priority"] > priority:
                insert_idx = i
                break
        self._data["tasks"].append(task)  # Append, then sort
        self._sort_tasks()
        self._save()
        logger.info(f"📋 Task added: [{task['id']}] {title[:60]} (priority={priority}, source={source})")
        return task

    def _sort_tasks(self):
        """Sort tasks: in_progress first, then queued by priority, then terminal states."""
        status_order = {
            self.STATUS_IN_PROGRESS: 0,
            self.STATUS_QUEUED: 1,
            self.STATUS_COMPLETED: 2,
            self.STATUS_FAILED: 3,
            self.STATUS_SKIPPED: 4,
        }
        self._data["tasks"].sort(key=lambda t: (
            status_order.get(t["status"], 9),
            t["priority"],
            t.get("created_at", ""),
        ))

    def get_current(self) -> Optional[Dict]:
        """Get the currently in-progress task, or None."""
        for task in self._data["tasks"]:
            if task["status"] == self.STATUS_IN_PROGRESS:
                return task
        return None

    def get_next_queued(self) -> Optional[Dict]:
        """Get the next queued task (highest priority, earliest creation)."""
        for task in self._data["tasks"]:
            if task["status"] == self.STATUS_QUEUED:
                return task
        return None

    def start_task(self, task_id: str) -> Optional[Dict]:
        """Mark a task as in_progress. Returns the task or None."""
        for task in self._data["tasks"]:
            if task["id"] == task_id and task["status"] == self.STATUS_QUEUED:
                task["status"] = self.STATUS_IN_PROGRESS
                task["started_at"] = datetime.now().isoformat()
                self._sort_tasks()
                self._save()
                logger.info(f"📋 Task started: [{task_id}] {task['title'][:60]}")
                return task
        return None

    def complete_task(self, task_id: str = None, summary: str = "",
                      gate_passed: bool = False,
                      bypass_verifier: bool = False) -> Optional[Dict]:
        """Mark a task as completed. If task_id is None, completes current task.

        `gate_passed=True` is the contract that the critic-gate ran and approved.
        Typed tasks with an expected artifact location may not complete without
        that stamp; otherwise agent-facing tools can bypass the daemon gate.

        For typed tasks with a code artifact (python_module, etc.), the task
        verifier runs before the COMPLETED transition. If the artifact fails
        verification (import error, failing tests), the task is moved to
        BLOCKED with the failure reason attached — not COMPLETED. This stops
        Andrew from marking code tasks done with failing tests.

        `bypass_verifier=True` is reserved for operator overrides and recovery
        paths; use sparingly and audit the resulting log entries.
        """
        task = self._find_task(task_id) if task_id else self.get_current()
        if task and task["status"] == self.STATUS_IN_PROGRESS:
            gate_required = bool(task.get("expected_location"))
            if gate_required and not gate_passed and not bypass_verifier:
                logger.warning(
                    f"🚫 complete_task blocked WITHOUT gate_passed for "
                    f"[{task['id']}] {task['title'][:60]} — critic gate required"
                )
                task["last_completion_block"] = {
                    "reason": "critic_gate_required",
                    "at": datetime.now().isoformat(),
                    "summary_attempt": (summary or "")[:200],
                }
                self._save()
                return None
            if not gate_passed:
                logger.warning(
                    f"⚠️ complete_task called WITHOUT gate_passed for "
                    f"[{task['id']}] {task['title'][:60]} — legacy untyped path used"
                )

            # ── Success-criterion verifier (for typed code tasks) ──────
            if not bypass_verifier and task.get("expected_artifact_type") == "python_module":
                try:
                    from repryntt.agents import task_types as _tt
                    v = _tt.verify_python_module(
                        task.get("expected_location", ""),
                        allow_no_tests=True,
                    )
                    task["last_verifier"] = v.to_dict()
                    if not v.passed:
                        task["status"] = self.STATUS_BLOCKED
                        task["completed_at"] = datetime.now().isoformat()
                        task["summary"] = (
                            f"Verifier blocked completion: {v.detail}. "
                            f"Original summary: {summary[:200]}"
                        )
                        self._sort_tasks()
                        self._save()
                        logger.warning(
                            f"🚫 Task BLOCKED at verifier: [{task['id']}] "
                            f"{task['title'][:60]} — {v.detail[:120]}"
                        )
                        return task
                except Exception:
                    logger.debug("Verifier hook failed (non-fatal); allowing completion", exc_info=True)

            task["status"] = self.STATUS_COMPLETED
            task["completed_at"] = datetime.now().isoformat()
            task["summary"] = summary or "Completed"
            task["gate_passed"] = bool(gate_passed)
            self._sort_tasks()
            self._save()
            logger.info(f"✅ Task completed: [{task['id']}] {task['title'][:60]}")
            return task
        return None

    def fail_task(self, task_id: str = None, reason: str = "") -> Optional[Dict]:
        """Mark a task as failed."""
        task = self._find_task(task_id) if task_id else self.get_current()
        if task and task["status"] == self.STATUS_IN_PROGRESS:
            task["status"] = self.STATUS_FAILED
            task["completed_at"] = datetime.now().isoformat()
            task["summary"] = reason or "Failed"
            self._sort_tasks()
            self._save()
            logger.info(f"❌ Task failed: [{task['id']}] {task['title'][:60]} — {reason[:80]}")
            return task
        return None

    def skip_task(self, task_id: str = None, reason: str = "") -> Optional[Dict]:
        """Skip a task (won't come back)."""
        task = self._find_task(task_id) if task_id else self.get_current()
        if task and task["status"] in (self.STATUS_IN_PROGRESS, self.STATUS_QUEUED):
            task["status"] = self.STATUS_SKIPPED
            task["completed_at"] = datetime.now().isoformat()
            task["summary"] = reason or "Skipped"
            self._sort_tasks()
            self._save()
            logger.info(f"⏭️ Task skipped: [{task['id']}] {task['title'][:60]}")
            return task
        return None

    def advance(self) -> Optional[Dict]:
        """Complete current task (if any) and start the next queued one.
        Returns the newly started task, or None if queue is empty."""
        current = self.get_current()
        if current:
            self.complete_task(current["id"])
        next_task = self.get_next_queued()
        if next_task:
            return self.start_task(next_task["id"])
        return None

    def _find_task(self, task_id: str) -> Optional[Dict]:
        for task in self._data["tasks"]:
            if task["id"] == task_id:
                return task
        return None

    # ── Query ────────────────────────────────────────────────

    def get_queue_prompt(self) -> str:
        """Generate a formatted prompt section showing the task queue state.

        This is the primary structural constraint: Andrew MUST work on the
        current task, not freestyle.
        """
        current = self.get_current()
        queued = [t for t in self._data["tasks"] if t["status"] == self.STATUS_QUEUED]
        completed = [t for t in self._data["tasks"] if t["status"] == self.STATUS_COMPLETED]

        parts = ["\n📋 **TASK QUEUE — YOUR WORK SCHEDULE**"]
        parts.append(f"   Completed: {len(completed)} | Remaining: {len(queued)} | Day: {self._data['day']}")

        if current:
            parts.append(f"\n   🔵 **CURRENT TASK** [{current['id']}] (priority {current['priority']}):")
            parts.append(f"   → {current['title']}")
            if current.get("description"):
                parts.append(f"     {current['description'][:200]}")
            parts.append(
                f"\n   ⚠️ YOU MUST WORK ON THIS TASK. Do not start something else."
                f"\n   When finished, the task will be auto-completed and the next one starts."
                f"\n   If this task is blocked, call `skip_task` with a reason."
            )
        elif queued:
            # No current task — show what's next (will be auto-started)
            next_t = queued[0]
            parts.append(f"\n   ⏳ **NEXT UP** [{next_t['id']}] (priority {next_t['priority']}):")
            parts.append(f"   → {next_t['title']}")
            if next_t.get("description"):
                parts.append(f"     {next_t['description'][:200]}")
        else:
            parts.append(
                "\n   ✅ All planned tasks complete for today."
                "\n   ⚠️ DO NOT free-associate or re-research topics you already covered."
                "\n   Instead, pick ONE of these concrete actions:"
                "\n   1. Check your active_projects.md and advance the next milestone"
                "\n   2. Write a new Python tool or script that does something useful"
                "\n   3. Use CodeForge to build a library (pass provider='nvidia')"
                "\n   4. Add a new project to active_projects.md with milestones"
                "\n   5. Check email and respond to anything pending"
                "\n   6. Use your senses (camera, mic, speaker) to interact with the physical world"
                "\n   7. Write a genuine reflection (3-5 sentences, not a research synthesis)"
                "\n   Whatever you pick: PRODUCE AN ARTIFACT. A file, a commit, a sent email."
                "\n   'Researching' or 'synthesizing' without writing runnable code = score 0."
            )

        # Show upcoming tasks (max 5)
        if len(queued) > 1:
            parts.append(f"\n   📝 **UPCOMING** ({len(queued) - 1} more):")
            for t in queued[1:6]:
                parts.append(f"   - [{t['id']}] {t['title'][:80]} (p{t['priority']})")
            if len(queued) > 6:
                parts.append(f"   ... and {len(queued) - 6} more")

        # Show today's completed work (brief)
        if completed:
            parts.append(f"\n   ✅ **DONE TODAY** ({len(completed)}):")
            for t in completed[-5:]:
                summary = t.get("summary", "")[:60]
                parts.append(f"   - {t['title'][:60]} → {summary}")

        parts.append("")  # trailing newline
        return "\n".join(parts)

    def get_stats(self) -> Dict:
        """Return queue statistics."""
        tasks = self._data["tasks"]
        return {
            "day": self._data["day"],
            "total": len(tasks),
            "queued": sum(1 for t in tasks if t["status"] == self.STATUS_QUEUED),
            "in_progress": sum(1 for t in tasks if t["status"] == self.STATUS_IN_PROGRESS),
            "completed": sum(1 for t in tasks if t["status"] == self.STATUS_COMPLETED),
            "failed": sum(1 for t in tasks if t["status"] == self.STATUS_FAILED),
            "skipped": sum(1 for t in tasks if t["status"] == self.STATUS_SKIPPED),
            "current_task": self.get_current(),
        }

    def get_all_tasks(self) -> List[Dict]:
        """Return all tasks (for tool display)."""
        return self._data["tasks"]
