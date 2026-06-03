"""
REPRYNTT Task System — Actionable Task Queue & Execution Engine

Replaces chain-of-thought exploration with concrete, actionable tasks.
The AI generates tasks it wants to accomplish. Users can inject tasks
with top priority. Tasks use chains/tools as execution mechanisms
but the top-level unit is always an actionable TASK with a deliverable.

Priority levels:
  0 = User-injected tasks (ALWAYS first)
  1 = Urgent system tasks
  2 = Daily plan tasks
  3 = AI autonomous tasks (self-generated)
  5 = Low priority / background tasks
"""

import json
import os
import time
import logging
import threading
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)

TASK_QUEUE_FILE = Path("brain/task_queue.json")
COMPLETED_TASKS_FILE = Path("brain/completed_tasks.json")
TASK_HISTORY_FILE = Path("brain/task_history.json")


class Task:
    """A single actionable task with a clear deliverable"""
    
    def __init__(self, title: str, description: str, priority: int = 3,
                 requested_by: str = "ai_autonomous", task_type: str = "general",
                 deliverable: str = "", tools_suggested: List[str] = None,
                 max_steps: int = 15, task_id: str = None,
                 assigned_agent: str = "",
                 expected_artifact_type: str = "",
                 expected_location: str = "",
                 downstream_consumer: str = "",
                 success_criterion: str = ""):
        self.id = task_id or f"task_{int(time.time())}_{hash(title) % 10000}"
        self.title = title
        self.description = description
        self.deliverable = deliverable  # What the completed task should produce
        self.priority = priority
        self.status = "queued"  # queued | in_progress | completed | failed | assigned | rejected
        self.requested_by = requested_by
        self.task_type = task_type  # research, code, creative, analysis, system, learning
        self.tools_suggested = tools_suggested or []
        self.max_steps = max_steps
        self.created_at = time.time()
        self.started_at = None
        self.completed_at = None
        self.chain_id = None  # Linked chain if multi-step
        self.result = None  # Final output/deliverable
        self.steps_taken = 0
        self.execution_log = []
        self.assigned_agent = assigned_agent  # agent_id of assigned agent (empty = unassigned)
        # Typed deliverable spec — gated at intake (see intake_gate.py)
        self.expected_artifact_type = expected_artifact_type
        self.expected_location = expected_location
        self.downstream_consumer = downstream_consumer
        self.success_criterion = success_criterion
        
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "deliverable": self.deliverable,
            "priority": self.priority,
            "status": self.status,
            "requested_by": self.requested_by,
            "task_type": self.task_type,
            "tools_suggested": self.tools_suggested,
            "max_steps": self.max_steps,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "chain_id": self.chain_id,
            "result": self.result,
            "steps_taken": self.steps_taken,
            "execution_log": self.execution_log[-10:],  # Keep last 10 entries
            "assigned_agent": self.assigned_agent,
            "expected_artifact_type": self.expected_artifact_type,
            "expected_location": self.expected_location,
            "downstream_consumer": self.downstream_consumer,
            "success_criterion": self.success_criterion,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Task':
        task = cls(
            title=data.get("title", "Untitled"),
            description=data.get("description", ""),
            priority=data.get("priority", 3),
            requested_by=data.get("requested_by", "unknown"),
            task_type=data.get("task_type", "general"),
            deliverable=data.get("deliverable", ""),
            tools_suggested=data.get("tools_suggested", []),
            max_steps=data.get("max_steps", 15),
            task_id=data.get("id"),
            expected_artifact_type=data.get("expected_artifact_type", ""),
            expected_location=data.get("expected_location", ""),
            downstream_consumer=data.get("downstream_consumer", ""),
            success_criterion=data.get("success_criterion", ""),
        )
        task.status = data.get("status", "queued")
        task.created_at = data.get("created_at", time.time())
        task.started_at = data.get("started_at")
        task.completed_at = data.get("completed_at")
        task.chain_id = data.get("chain_id")
        task.result = data.get("result")
        task.steps_taken = data.get("steps_taken", 0)
        task.execution_log = data.get("execution_log", [])
        task.assigned_agent = data.get("assigned_agent", "")
        return task


class TaskSystem:
    """
    Manages the actionable task queue for REPRYNTT.
    
    Tasks are the primary unit of work. The AI generates tasks during
    morning planning and self-prompting. Users inject tasks via chat.
    Chains of thought are used internally to execute multi-step tasks.
    """
    
    def __init__(self):
        self._lock = threading.Lock()
        self._queue: List[Task] = []
        self._active_task: Optional[Task] = None
        self._load_queue()
        logger.info(f"📋 TaskSystem initialized: {len(self._queue)} queued tasks, "
                    f"active={'YES' if self._active_task else 'none'}")
    
    def _load_queue(self):
        """Load task queue from disk"""
        try:
            if TASK_QUEUE_FILE.exists():
                with open(TASK_QUEUE_FILE, 'r') as f:
                    data = json.load(f)
                
                tasks = data if isinstance(data, list) else data.get("tasks", [])
                for task_data in tasks:
                    task = Task.from_dict(task_data)
                    if task.status == "in_progress":
                        self._active_task = task
                    elif task.status in ("queued", "assigned"):
                        self._queue.append(task)
                
                logger.info(f"📂 Loaded {len(self._queue)} queued tasks from disk")
        except Exception as e:
            logger.warning(f"⚠️ Could not load task queue: {e}")
    
    def _save_queue(self):
        """Persist task queue to disk"""
        try:
            TASK_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
            all_tasks = list(self._queue)
            if self._active_task:
                all_tasks.insert(0, self._active_task)
            
            with open(TASK_QUEUE_FILE, 'w') as f:
                json.dump({
                    "updated_at": time.time(),
                    "active_task": self._active_task.id if self._active_task else None,
                    "queue_size": len(self._queue),
                    "tasks": [t.to_dict() for t in all_tasks]
                }, f, indent=2)
        except Exception as e:
            logger.error(f"❌ Could not save task queue: {e}")
    
    def _save_completed(self, task: Task):
        """Append completed task to history"""
        try:
            COMPLETED_TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
            completed = []
            if COMPLETED_TASKS_FILE.exists():
                with open(COMPLETED_TASKS_FILE, 'r') as f:
                    completed = json.load(f)
            
            completed.append(task.to_dict())
            
            # Keep last 200 completed tasks
            if len(completed) > 200:
                completed = completed[-200:]
            
            with open(COMPLETED_TASKS_FILE, 'w') as f:
                json.dump(completed, f, indent=2)
        except Exception as e:
            logger.error(f"❌ Could not save completed task: {e}")
    
    # ─── Task Creation ────────────────────────────────────────────
    
    def _was_recently_completed(self, title: str, hours: float = 2.0) -> bool:
        """Check if a task with this title was completed in the last N hours"""
        try:
            if not COMPLETED_TASKS_FILE.exists():
                return False
            with open(COMPLETED_TASKS_FILE, 'r') as f:
                completed = json.load(f)
            
            cutoff = time.time() - (hours * 3600)
            title_lower = title.lower().strip()
            
            for task_data in reversed(completed):  # Most recent first
                completed_at = task_data.get('completed_at', 0)
                if completed_at < cutoff:
                    break  # Older than cutoff, stop checking
                if task_data.get('title', '').lower().strip() == title_lower:
                    return True
            return False
        except Exception:
            return False
    
    def get_recently_completed_titles(self, hours: float = 4.0) -> List[str]:
        """Get titles of tasks completed in the last N hours"""
        try:
            if not COMPLETED_TASKS_FILE.exists():
                return []
            with open(COMPLETED_TASKS_FILE, 'r') as f:
                completed = json.load(f)
            
            cutoff = time.time() - (hours * 3600)
            titles = []
            for task_data in reversed(completed):
                completed_at = task_data.get('completed_at', 0)
                if completed_at < cutoff:
                    break
                titles.append(task_data.get('title', ''))
            return titles
        except Exception:
            return []

    def create_task(self, title: str, description: str, priority: int = 3,
                    requested_by: str = "ai_autonomous", task_type: str = "general",
                    deliverable: str = "", tools_suggested: List[str] = None,
                    max_steps: int = 15,
                    expected_artifact_type: str = "",
                    expected_location: str = "",
                    downstream_consumer: str = "",
                    success_criterion: str = "",
                    bypass_intake: bool = False) -> Task:
        """Create and queue a new task.

        Typed deliverable fields (expected_artifact_type/_location, downstream_consumer,
        success_criterion) are gated by intake_gate.check_admissibility — see Step 0
        of the critic-gate plan. Rejected tasks are returned with status="rejected"
        and `result` populated with the rejection reasons; they are NOT queued.

        `bypass_intake=True` is reserved for internal callers (recovery rounds, the
        one-shot backfill migration). Do not expose it to Andrew's tool surface.
        """
        # ── Type inference: fill blanks from title/description ─────────
        # Caller-supplied fields always win. Inference covers the common case
        # where Andrew writes "create foo.py with bar() function" but leaves
        # typed fields blank — we don't want the intake gate to advise-mode
        # it out, and we don't want self-eval to have nothing to bind to.
        try:
            from repryntt.agents import task_types as _tt
            _inferred = _tt.infer_type(title, description)
            existing_fields = {
                "expected_artifact_type": expected_artifact_type,
                "expected_location": expected_location,
                "success_criterion": success_criterion,
            }
            merged = _tt.merge_with_existing(existing_fields, _inferred)
            expected_artifact_type = merged.get("expected_artifact_type", expected_artifact_type)
            expected_location = merged.get("expected_location", expected_location)
            success_criterion = merged.get("success_criterion", success_criterion)
        except Exception:
            logger.debug("Task type inference failed (non-fatal)", exc_info=True)

        # ── Intake admissibility gate ───────────────────────────────────
        if not bypass_intake:
            try:
                from repryntt.agents.intake_gate import check_admissibility
            except ImportError:
                from .intake_gate import check_admissibility
            verdict = check_admissibility({
                "title": title,
                "description": description,
                "expected_artifact_type": expected_artifact_type,
                "expected_location": expected_location,
                "downstream_consumer": downstream_consumer,
                "success_criterion": success_criterion,
            }, strict=True)
            if not verdict["accepted"]:
                rejected = Task(
                    title=title, description=description, priority=priority,
                    requested_by=requested_by, task_type=task_type,
                    expected_artifact_type=expected_artifact_type,
                    expected_location=expected_location,
                    downstream_consumer=downstream_consumer,
                    success_criterion=success_criterion,
                )
                rejected.status = "rejected"
                rejected.result = "Intake rejected: " + " | ".join(verdict["reasons"])
                logger.info(f"❌ Task rejected at intake: {title!r}")
                return rejected

        with self._lock:
            # Deduplicate — skip if same title already queued or active
            existing = [t for t in self._queue if t.title.lower() == title.lower()]
            if existing:
                logger.info(f"⏭️ Task already queued: {title}")
                return existing[0]
            if self._active_task and self._active_task.title.lower() == title.lower():
                logger.info(f"⏭️ Task already active: {title}")
                return self._active_task

            # Deduplicate against recently completed tasks (last 2 hours)
            # User tasks (priority 0) bypass this check
            if priority > 0 and self._was_recently_completed(title, hours=2.0):
                logger.info(f"⏭️ Task recently completed, skipping: {title}")
                # Return a dummy completed task so callers don't break
                dummy = Task(title=title, description=description, priority=priority,
                            requested_by=requested_by, task_type=task_type)
                dummy.status = "completed"
                dummy.result = "Skipped — recently completed"
                return dummy

            task = Task(
                title=title,
                description=description,
                priority=priority,
                requested_by=requested_by,
                task_type=task_type,
                deliverable=deliverable,
                tools_suggested=tools_suggested,
                max_steps=max_steps,
                expected_artifact_type=expected_artifact_type,
                expected_location=expected_location,
                downstream_consumer=downstream_consumer,
                success_criterion=success_criterion,
            )
            self._queue.append(task)
            self._sort_queue()
            self._save_queue()

            logger.info(f"📋 Task created: [{task.priority}] {task.title} (by {requested_by})")
            return task
    
    def inject_user_task(self, title: str, description: str = "",
                         task_type: str = "general",
                         expected_artifact_type: str = "",
                         expected_location: str = "",
                         downstream_consumer: str = "",
                         success_criterion: str = "") -> Task:
        """
        Inject a user task with HIGHEST priority (0).
        User tasks always get processed first.

        The four typed deliverable fields go through the same intake gate as
        autonomous tasks. If the user (or a dispatch caller) omits them, the
        task is returned with status='rejected' and reasons attached.
        """
        if not description:
            description = title

        task = self.create_task(
            title=title,
            description=description,
            priority=0,  # User tasks = top priority
            requested_by="user",
            task_type=task_type,
            deliverable=f"Complete the user's request: {title}",
            max_steps=20,
            expected_artifact_type=expected_artifact_type,
            expected_location=expected_location,
            downstream_consumer=downstream_consumer,
            success_criterion=success_criterion,
        )
        if task.status == "rejected":
            logger.warning(f"🔴 USER TASK REJECTED at intake: {title} — {task.result}")
        else:
            logger.info(f"🔴 USER TASK INJECTED (priority 0): {title}")
        return task
    
    def create_tasks_from_plan(self, tasks_data: List[dict]) -> List[Task]:
        """Create multiple tasks from a daily plan"""
        created = []
        for i, td in enumerate(tasks_data):
            task = self.create_task(
                title=td.get("title", f"Plan Task #{i+1}"),
                description=td.get("description", ""),
                priority=td.get("priority", 2),  # Daily plan = priority 2
                requested_by=td.get("requested_by", "daily_plan"),
                task_type=td.get("task_type", "general"),
                deliverable=td.get("deliverable", ""),
                tools_suggested=td.get("tools", []),
                max_steps=td.get("max_steps", 15)
            )
            created.append(task)
        logger.info(f"📋 Created {len(created)} tasks from daily plan")
        return created
    
    # ─── Task Execution Flow ──────────────────────────────────────
    
    def get_next_task(self) -> Optional[Task]:
        """
        Get the next task to work on.
        Returns None if queue is empty.
        Does NOT start the task — call start_task() for that.
        """
        with self._lock:
            if self._active_task:
                return self._active_task
            if not self._queue:
                return None
            return self._queue[0]
    
    def start_task(self, task: Task) -> bool:
        """Mark a task as in-progress and set it as the active task"""
        with self._lock:
            if self._active_task and self._active_task.id != task.id:
                logger.warning(f"⚠️ Cannot start task {task.title} — "
                             f"task {self._active_task.title} is still active")
                return False
            
            task.status = "in_progress"
            task.started_at = time.time()
            self._active_task = task
            
            # Remove from queue if it was there
            self._queue = [t for t in self._queue if t.id != task.id]
            
            self._save_queue()
            logger.info(f"▶️ Task started: {task.title}")
            return True
    
    def complete_task(self, task: Task, result: str = "") -> bool:
        """Mark a task as completed with its result"""
        with self._lock:
            task.status = "completed"
            task.completed_at = time.time()
            task.result = result
            
            if self._active_task and self._active_task.id == task.id:
                self._active_task = None
            
            self._save_completed(task)
            self._save_queue()
            
            elapsed = task.completed_at - (task.started_at or task.created_at)
            logger.info(f"✅ Task completed: {task.title} ({elapsed:.1f}s, {task.steps_taken} steps)")
            return True
    
    def fail_task(self, task: Task, error: str = "") -> bool:
        """Mark a task as failed"""
        with self._lock:
            task.status = "failed"
            task.completed_at = time.time()
            task.result = f"FAILED: {error}"
            task.execution_log.append({"event": "failed", "error": error, "at": time.time()})
            
            if self._active_task and self._active_task.id == task.id:
                self._active_task = None
            
            self._save_completed(task)
            self._save_queue()
            
            logger.warning(f"❌ Task failed: {task.title} — {error}")
            return True
    
    def log_task_progress(self, task: Task, message: str):
        """Log progress on a task"""
        task.steps_taken += 1
        task.execution_log.append({
            "step": task.steps_taken,
            "message": message[:200],
            "at": time.time()
        })
        self._save_queue()
    
    def link_chain(self, task: Task, chain_id: str):
        """Link a chain of thought to this task (for multi-step execution)"""
        task.chain_id = chain_id
        task.execution_log.append({
            "event": "chain_linked",
            "chain_id": chain_id,
            "at": time.time()
        })
        self._save_queue()
        logger.info(f"🔗 Task '{task.title}' linked to chain {chain_id}")
    
    # ─── Task Query ───────────────────────────────────────────────
    
    def get_active_task(self) -> Optional[Task]:
        """Get the currently active task, if any"""
        return self._active_task
    
    def get_queue(self) -> List[Task]:
        """Get all queued tasks (sorted by priority)"""
        return list(self._queue)
    
    def get_queue_summary(self) -> str:
        """Get a human-readable summary of the task queue"""
        lines = []
        if self._active_task:
            t = self._active_task
            elapsed = time.time() - (t.started_at or t.created_at)
            lines.append(f"🔄 ACTIVE: [{t.priority}] {t.title} ({elapsed:.0f}s, {t.steps_taken} steps)")
        
        if self._queue:
            lines.append(f"\n📋 QUEUED ({len(self._queue)}):")
            for i, t in enumerate(self._queue[:10]):
                by = f"👤USER" if t.requested_by == "user" else t.requested_by
                lines.append(f"  {i+1}. [{t.priority}] {t.title} (by {by})")
            if len(self._queue) > 10:
                lines.append(f"  ... and {len(self._queue) - 10} more")
        else:
            lines.append("📋 Queue is empty")
        
        return "\n".join(lines)
    
    def has_user_tasks(self) -> bool:
        """Check if there are any priority-0 user tasks waiting"""
        if self._active_task and self._active_task.requested_by == "user":
            return True
        return any(t.requested_by == "user" for t in self._queue)
    
    def queue_size(self) -> int:
        """Total tasks in queue (not counting active)"""
        return len(self._queue)
    
    def _sort_queue(self):
        """Sort queue: priority ASC (0 first), then creation time ASC"""
        self._queue.sort(key=lambda t: (t.priority, t.created_at))

    # ─── Agent Assignment ─────────────────────────────────────────

    def assign_task_to_agent(self, task_id: str, agent_id: str) -> bool:
        """Assign a queued task to a specific agent."""
        with self._lock:
            # Check active task
            if self._active_task and self._active_task.id == task_id:
                self._active_task.assigned_agent = agent_id
                self._active_task.status = "assigned"
                self._active_task.execution_log.append({
                    "event": "assigned",
                    "agent_id": agent_id,
                    "at": time.time()
                })
                self._save_queue()
                logger.info(f"📌 Task '{self._active_task.title}' assigned to agent {agent_id}")
                return True

            # Check queued tasks
            for task in self._queue:
                if task.id == task_id:
                    task.assigned_agent = agent_id
                    task.status = "assigned"
                    task.execution_log.append({
                        "event": "assigned",
                        "agent_id": agent_id,
                        "at": time.time()
                    })
                    self._save_queue()
                    logger.info(f"📌 Task '{task.title}' assigned to agent {agent_id}")
                    return True

            logger.warning(f"⚠️ Task {task_id} not found for assignment")
            return False

    def get_tasks_for_agent(self, agent_id: str) -> List[Task]:
        """Get all tasks assigned to a specific agent (priority order)."""
        tasks = []
        with self._lock:
            if self._active_task and self._active_task.assigned_agent == agent_id:
                tasks.append(self._active_task)
            for task in self._queue:
                if task.assigned_agent == agent_id and task.status in ("assigned", "queued"):
                    tasks.append(task)
        return tasks

    def get_unassigned_tasks(self) -> List[Task]:
        """Get all queued tasks that haven't been assigned to an agent yet."""
        with self._lock:
            return [t for t in self._queue
                    if not t.assigned_agent and t.status == "queued"]

    def get_task_by_id(self, task_id: str) -> Optional[Task]:
        """Look up a task by its ID (active or queued)."""
        with self._lock:
            if self._active_task and self._active_task.id == task_id:
                return self._active_task
            for task in self._queue:
                if task.id == task_id:
                    return task
        # Check completed tasks
        try:
            if COMPLETED_TASKS_FILE.exists():
                with open(COMPLETED_TASKS_FILE, 'r') as f:
                    completed = json.load(f)
                for td in reversed(completed):
                    if td.get("id") == task_id:
                        return Task.from_dict(td)
        except Exception:
            pass
        return None

    def reload_queue(self):
        """Force reload the task queue from disk (for cross-process sync)."""
        with self._lock:
            self._queue = []
            self._active_task = None
            self._load_queue()
            logger.info(f"🔄 Task queue reloaded: {len(self._queue)} queued, "
                        f"active={'YES' if self._active_task else 'none'}")

    # ─── Task Prompt Building ─────────────────────────────────────
    
    def build_task_execution_prompt(self, task: Task, identity: str = "",
                                     available_tools: str = "") -> str:
        """
        Build an action-oriented prompt for the AI to execute a task.
        This replaces the exploration-oriented chain prompts.
        """
        prompt_parts = []
        
        # Identity layer
        if identity:
            prompt_parts.append(identity)
        
        # Task directive - action-oriented, not exploration-oriented
        prompt_parts.append(f"""
═══ ACTIVE TASK ═══
Title: {task.title}
Description: {task.description}
Type: {task.task_type}
Expected Deliverable: {task.deliverable}
Priority: {task.priority} ({'USER REQUEST — TOP PRIORITY' if task.requested_by == 'user' else task.requested_by})
Steps taken so far: {task.steps_taken}/{task.max_steps}
""")
        
        # Progress context
        if task.execution_log:
            recent = task.execution_log[-3:]
            prompt_parts.append("Recent progress:")
            for entry in recent:
                if 'message' in entry:
                    prompt_parts.append(f"  • {entry['message']}")
        
        # Action directive
        remaining = task.max_steps - task.steps_taken
        if remaining <= 2:
            prompt_parts.append(f"""
⚠️ You have {remaining} step(s) remaining. FINALIZE your work now.
Produce your deliverable and output: TASK COMPLETE: <your result>
""")
        elif task.steps_taken == 0:
            prompt_parts.append(f"""
📌 STEP 1 — GET STARTED:
- This is your FIRST step. You MUST call a tool to begin work.
- Do NOT write a final answer yet. Focus on gathering information or creating initial output.
- USE a tool now. Tools like grokipedia_search, write_file, etc. are available via the API.
- You have {remaining} steps remaining. Use them wisely.
""")
        else:
            prompt_parts.append(f"""
📌 STEP {task.steps_taken + 1} INSTRUCTIONS:
- Continue making progress on this task
- USE TOOLS to accomplish real work (search, write files, analyze data, etc.)
- Build on your previous work — do NOT repeat what is already done
- You have {remaining} steps remaining
- You may output TASK COMPLETE: <result> only when you have actually finished the work
""")
        
        # Available tools — always include tool usage reference
        if available_tools:
            prompt_parts.append(f"\nAvailable tools:\n{available_tools}")
        elif task.tools_suggested:
            prompt_parts.append(f"\nSuggested tools: {', '.join(task.tools_suggested)}")
        
        # Always include tool awareness so the AI knows what tools are available
        prompt_parts.append("""
HOW TO USE TOOLS:
Your tools are available via the API. Call them by name with appropriate parameters.

Key tools:
• grokipedia_search — Search for info
• write_file — Create a file
• run_terminal_cmd — Run command
• store_learning — Save to memory
• mcp_fetch_fetch — Fetch web page
• quick_research — Send 3 agents to research
• quick_brainstorm — 5 agents brainstorm ideas
• create_swarm — Create agent team
• start_discussion — Agent discussion

Call at least one tool per step. Do not just describe what you would do — actually call the tool.
""")
        
        return "\n".join(prompt_parts)
    
    def build_task_generation_prompt(self, context: dict = None) -> str:
        """
        Build prompt for AI to generate its own task list.
        Used during morning startup and self-prompting.
        """
        ctx = context or {}
        completed_recently = ctx.get("completed_tasks", [])
        current_goals = ctx.get("goals", [])
        available_tools_summary = ctx.get("tools_summary", "")
        hormone_state = ctx.get("hormone_state", "")
        values_context = ctx.get("values_context") or self._load_bootstrap_text("VALUES.md", 2600)
        interests_context = ctx.get("interests_context") or self._load_bootstrap_text("INTERESTS.md", 2600)
        daily_seed_context = ctx.get("daily_seed_context") or self._load_daily_seed_context()
        
        prompt = f"""Create a list of ACTIONABLE AUTONOMOUS TASKS for Andrew.
Every task must produce something useful — not research for research's sake.

Ground task choice in these sources, in this order:
1. VALUES.md: the Two Questions, operator priorities, anti-priorities, and deliverable standards.
2. INTERESTS.md: Andrew's specific curiosity questions and build ideas.
3. Daily seeds/news: current world, AI, science, engineering, and major-event context.

Prefer tasks that combine at least two of those sources. A strong task says:
- what real human/Earth problem, interest question, or current event it connects to
- what artifact it will produce
- how Andrew can verify it with tools, sources, code, or measurable output

Your REAL capabilities:
- Trading: trading_scan, trading_signals, dexscreener_trending, sim_portfolio
- Commerce: commerce_shopify, commerce_etsy, commerce_analytics
- System: run_terminal_cmd, read_file, write_file
- Web search: grokipedia_search, call_jarvis (for web search via cloud AI)
- Memory: store_learning, search_knowledge

GOOD tasks:
- "Build a water-access data table from today's drought seed and cite sources"
- "Prototype a small energy-storage calculator from VALUES.md Two Questions"
- "Deep-dive one INTERESTS.md AI question and produce a tested Python demo"
- "Analyze today's AI news against Andrew's autonomous-agent interests with sources"
- "Check system health only when reliability signals or operator instructions justify it"

BANNED tasks (DO NOT generate these):
- Generic summaries with no artifact, test, source table, or model
- Repryntt internals, bootstrap rewrites, or monitoring busywork unless operator assigned or reliability is actually failing
- Repeating completed topics from the recent list
- Philosophical navel-gazing without a concrete deliverable
- ANY task where the deliverable is just "stored in knowledge base"

"""
        if values_context:
            prompt += f"VALUES.md excerpt:\n{values_context}\n\n"

        if interests_context:
            prompt += f"INTERESTS.md excerpt:\n{interests_context}\n\n"

        if daily_seed_context:
            prompt += f"Today's daily seeds/news context:\n{daily_seed_context}\n\n"

        if completed_recently:
            prompt += "Recently completed tasks (DO NOT repeat ANY of these — generate DIFFERENT tasks):\n"
            for ct in completed_recently[-15:]:
                prompt += f"  ✅ {ct}\n"
            prompt += "\nIMPORTANT: Every task you generate MUST have a DIFFERENT title from the ones above.\n\n"
        
        if current_goals:
            prompt += "Current goals to consider:\n"
            for g in current_goals:
                prompt += f"  🎯 {g}\n"
            prompt += "\n"
        
        if hormone_state:
            prompt += f"Current emotional state: {hormone_state}\n\n"
        
        if available_tools_summary:
            prompt += f"Tools you can use: {available_tools_summary}\n\n"
        
        prompt += """Generate 3-5 actionable tasks. Respond in this EXACT JSON format:
{
  "tasks": [
    {
      "title": "Short action-oriented title",
      "description": "What specifically to do and how",
      "deliverable": "What the completed task produces",
      "task_type": "research|code|creative|analysis|system|learning",
      "tools": ["tool1", "tool2"],
      "priority": 3,
      "requested_by": "ai_autonomous",
      "max_steps": 5
    }
  ]
}

Respond ONLY with the JSON. No other text."""
        
        return prompt

    @staticmethod
    def _load_bootstrap_text(file_name: str, max_chars: int = 2000) -> str:
        """Load Andrew bootstrap context from the live home dir, with repo fallback."""
        candidates = [
            Path.home() / ".repryntt" / "brain" / "bootstrap" / file_name,
            Path("brain") / "bootstrap" / file_name,
        ]
        for path in candidates:
            try:
                if path.exists():
                    return path.read_text(encoding="utf-8").strip()[:max_chars]
            except Exception:
                continue
        return ""

    @staticmethod
    def _load_daily_seed_context(max_items: int = 8) -> str:
        """Load compact daily seed/news context for autonomous task generation."""
        seed_path = (
            Path.home()
            / ".repryntt"
            / "workspace"
            / "agents"
            / "operator"
            / "seeds"
            / f"daily_seeds_{datetime.now().date().isoformat()}.json"
        )
        try:
            if not seed_path.exists():
                return ""
            data = json.loads(seed_path.read_text(encoding="utf-8"))
            lines = []
            for seed in data.get("all_seeds", [])[:max_items]:
                domain = seed.get("domain", "seed")
                text = (seed.get("text") or seed.get("headline") or "").strip()
                if text:
                    lines.append(f"- [{domain}] {text[:220]}")
            return "\n".join(lines)
        except Exception:
            return ""

    # ------------------------------------------------------------------ #
    #  PERSISTENT (LOCKED) TASKS — reasoning_chain.json                    #
    # ------------------------------------------------------------------ #

    def create_persistent_task(self, goal: str = "", success_criteria: str = "",
                               max_steps: int = 20,
                               expected_artifact_type: str = "",
                               expected_location: str = "",
                               downstream_consumer: str = "",
                               **kwargs) -> str:
        """Create a LOCKED persistent task that persists across heartbeats.

        The system will NOT let you stop or switch until you complete the goal
        (positive outcome) or determine it's impossible (negative outcome).

        Args:
            goal: Clear description of what to achieve. Required.
            success_criteria: How to know the goal is met. Required.
            expected_artifact_type: Typed deliverable kind (code, analysis_md, ...).
            expected_location: Where the artifact will land. Operator-visible.
            downstream_consumer: Who will use it (operator, customer, ...).
            max_steps: Safety cap — max heartbeats before auto-timeout (default 20).
        """
        if not goal:
            return json.dumps({"error": "goal is required"})
        if not success_criteria:
            return json.dumps({"error": "success_criteria is required"})

        # ── Intake admissibility gate (same as create_task) ─────────────
        try:
            from repryntt.agents.intake_gate import check_admissibility
        except ImportError:
            from .intake_gate import check_admissibility
        verdict = check_admissibility({
            "title": goal,
            "description": goal,
            "expected_artifact_type": expected_artifact_type,
            "expected_location": expected_location,
            "downstream_consumer": downstream_consumer,
            "success_criterion": success_criteria,
        }, strict=True)
        if not verdict["accepted"]:
            return json.dumps({
                "error": "Persistent task rejected at intake",
                "reasons": verdict["reasons"],
                "guidance": (
                    "Persistent tasks must declare expected_artifact_type, expected_location "
                    "(operator-visible), downstream_consumer (not 'self'/'andrew'), and "
                    "success_criteria free of operator-blocklisted vocabulary "
                    "(if the operator has configured a blocklist). Rewrite the goal in terms of an "
                    "external deliverable a human or business would use."
                ),
            })

        import datetime as _dt
        chain_path = os.path.expanduser(
            "~/.repryntt/workspace/agents/operator/reasoning_chain.json"
        )

        if os.path.exists(chain_path):
            try:
                with open(chain_path, "r") as f:
                    existing = json.load(f)
                if existing.get("status") == "active":
                    return json.dumps({
                        "error": "An active chain already exists. Complete or close it first.",
                        "existing_chain": {
                            "topic": existing.get("topic", "?"),
                            "goal": existing.get("goal", ""),
                            "goal_type": existing.get("goal_type", "flexible"),
                            "steps": len(existing.get("steps_completed", [])),
                        },
                    })
            except Exception:
                pass

        new_chain = {
            "status": "active",
            "topic": goal[:200],
            "goal": goal,
            "goal_type": "locked",
            "success_criteria": success_criteria,
            "created": _dt.datetime.now().isoformat(),
            "target_steps": max(3, min(20, int(max_steps))),
            "steps_completed": [],
            "next_step_hint": f"Begin working on: {goal}",
            "last_evaluation": "",
            "expected_artifact_type": expected_artifact_type,
            "expected_location": expected_location,
            "downstream_consumer": downstream_consumer,
        }

        os.makedirs(os.path.dirname(chain_path), exist_ok=True)
        with open(chain_path, "w") as f:
            json.dump(new_chain, f, indent=2, default=str)

        return json.dumps({
            "status": "created",
            "goal": goal,
            "success_criteria": success_criteria,
            "goal_type": "locked",
            "max_steps": new_chain["target_steps"],
            "message": (
                "Locked persistent task created. It will persist across heartbeats "
                "and override normal priorities until you reach a definitive outcome. "
                "Call complete_persistent_task when done."
            ),
        })

    def complete_persistent_task(self, outcome: str = "", detail: str = "", **kwargs) -> str:
        """Complete (close) the active persistent task with a definitive outcome.

        Args:
            outcome: 'positive' | 'negative' | 'partial'.
            detail: Explanation of the outcome. Required.
        """
        if outcome not in ("positive", "negative", "partial"):
            return json.dumps({"error": "outcome must be 'positive', 'negative', or 'partial'"})
        if not detail:
            return json.dumps({"error": "detail is required"})

        import datetime as _dt
        chain_path = os.path.expanduser(
            "~/.repryntt/workspace/agents/operator/reasoning_chain.json"
        )

        if not os.path.exists(chain_path):
            return json.dumps({"error": "No active reasoning chain found"})

        try:
            with open(chain_path, "r") as f:
                chain = json.load(f)
        except Exception as e:
            return json.dumps({"error": f"Failed to load chain: {e}"})

        if chain.get("status") != "active":
            return json.dumps({"error": "No active chain to complete"})

        chain["status"] = "completed"
        chain["outcome"] = outcome
        chain["outcome_detail"] = detail
        chain["completed_at"] = _dt.datetime.now().isoformat()
        steps = len(chain.get("steps_completed", []))

        archive_dir = os.path.expanduser(
            "~/.repryntt/workspace/agents/operator/completed_tasks"
        )
        os.makedirs(archive_dir, exist_ok=True)
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        archive_path = os.path.join(archive_dir, f"task_{ts}_{outcome}.json")
        with open(archive_path, "w") as f:
            json.dump(chain, f, indent=2, default=str)

        os.remove(chain_path)

        return json.dumps({
            "status": "completed",
            "outcome": outcome,
            "detail": detail,
            "steps_taken": steps,
            "goal": chain.get("goal", chain.get("topic", "")),
            "archived_to": archive_path,
            "message": f"Task completed with {outcome} outcome after {steps} steps.",
        })
