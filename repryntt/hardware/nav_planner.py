"""
repryntt.hardware.nav_planner — Multi-Step Exploration Chain Planner.

Gives Andrew the ability to form and execute complex spatial plans:
    "Go down the hall, turn right, check the second door on the left"

Instead of single nav_set_intent commands that expire after N steps,
a NavPlan is a sequence of spatial actions with completion conditions.
The planner monitors progress and auto-advances through the chain.

Architecture:
    Andrew (brain) → NavPlan (plan) → Explorer (body)
        ↑                                   ↓
        └─── status updates ←── completion checks

Each step in a plan has:
    - An action: go_direction, goto_room, goto_landmark, goto_coords, look_around
    - A completion condition: distance_traveled, scene_type_reached, steps_taken, landmark_found
    - A timeout: max steps before moving to next action
    - An optional note: what Andrew expects to see/find

The planner runs inside the explorer loop, checking completion
conditions each step and advancing to the next action when met.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

PLAN_FILE = Path.home() / ".repryntt" / "brain" / "nav_plan.json"


class StepStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


class CompletionType(str, Enum):
    STEPS_TAKEN = "steps_taken"
    DISTANCE_CM = "distance_cm"
    SCENE_TYPE = "scene_type"
    LANDMARK_FOUND = "landmark_found"
    TIMEOUT = "timeout"


@dataclass
class PlanStep:
    """A single step in a multi-step exploration plan."""
    step_id: int
    action: str                    # go_forward, go_left, go_right, go_backward,
                                   # goto_room, goto_landmark, goto_coords, look_around
    description: str               # human-readable: "go down the hallway"
    direction: str = "forward"     # for go_* actions
    target: str = ""               # for goto_* actions (room type, landmark id, coords)
    completion: CompletionType = CompletionType.STEPS_TAKEN
    completion_value: Any = 20     # steps, cm, scene_type string, etc
    max_steps: int = 50            # timeout — move to next step if this many steps pass
    note: str = ""                 # what Andrew expects to find
    status: StepStatus = StepStatus.PENDING
    steps_executed: int = 0
    distance_traveled: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    completion_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "action": self.action,
            "description": self.description,
            "direction": self.direction,
            "target": self.target,
            "completion": self.completion.value,
            "completion_value": self.completion_value,
            "max_steps": self.max_steps,
            "note": self.note,
            "status": self.status.value,
            "steps_executed": self.steps_executed,
            "distance_traveled": round(self.distance_traveled, 1),
            "completion_reason": self.completion_reason,
        }

    @staticmethod
    def from_dict(data: Dict) -> "PlanStep":
        return PlanStep(
            step_id=data.get("step_id", 0),
            action=data.get("action", "go_forward"),
            description=data.get("description", ""),
            direction=data.get("direction", "forward"),
            target=data.get("target", ""),
            completion=CompletionType(data.get("completion", "steps_taken")),
            completion_value=data.get("completion_value", 20),
            max_steps=data.get("max_steps", 50),
            note=data.get("note", ""),
            status=StepStatus(data.get("status", "pending")),
            steps_executed=data.get("steps_executed", 0),
            distance_traveled=data.get("distance_traveled", 0),
            started_at=data.get("started_at", 0),
            completed_at=data.get("completed_at", 0),
            completion_reason=data.get("completion_reason", ""),
        )


@dataclass
class NavPlan:
    """A multi-step spatial exploration plan."""
    plan_id: str
    goal: str                              # high-level: "explore the kitchen area"
    steps: List[PlanStep] = field(default_factory=list)
    current_step_idx: int = 0
    status: str = "active"                 # active, completed, cancelled
    created_at: float = 0.0
    completed_at: float = 0.0
    total_steps_executed: int = 0
    total_distance_cm: float = 0.0
    findings: List[str] = field(default_factory=list)

    def current_step(self) -> Optional[PlanStep]:
        if 0 <= self.current_step_idx < len(self.steps):
            return self.steps[self.current_step_idx]
        return None

    def advance(self, reason: str = "") -> Optional[PlanStep]:
        """Mark current step complete and move to next."""
        current = self.current_step()
        if current:
            current.status = StepStatus.COMPLETED
            current.completed_at = time.time()
            current.completion_reason = reason

        self.current_step_idx += 1
        if self.current_step_idx >= len(self.steps):
            self.status = "completed"
            self.completed_at = time.time()
            return None

        next_step = self.steps[self.current_step_idx]
        next_step.status = StepStatus.ACTIVE
        next_step.started_at = time.time()
        return next_step

    def cancel(self, reason: str = ""):
        self.status = "cancelled"
        self.completed_at = time.time()
        current = self.current_step()
        if current and current.status == StepStatus.ACTIVE:
            current.status = StepStatus.SKIPPED
            current.completion_reason = reason

    def progress_summary(self) -> str:
        completed = sum(1 for s in self.steps
                        if s.status == StepStatus.COMPLETED)
        total = len(self.steps)
        current = self.current_step()
        lines = [
            f"Plan: {self.goal}",
            f"Progress: {completed}/{total} steps | "
            f"Status: {self.status}",
        ]
        if current:
            lines.append(
                f"Current: Step {current.step_id + 1} — {current.description} "
                f"({current.steps_executed} steps, "
                f"{current.distance_traveled:.0f}cm)")
        if self.findings:
            lines.append(f"Findings: {'; '.join(self.findings[-3:])}")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "goal": self.goal,
            "steps": [s.to_dict() for s in self.steps],
            "current_step_idx": self.current_step_idx,
            "status": self.status,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "total_steps_executed": self.total_steps_executed,
            "total_distance_cm": round(self.total_distance_cm, 1),
            "findings": self.findings[-10:],
        }

    @staticmethod
    def from_dict(data: Dict) -> "NavPlan":
        plan = NavPlan(
            plan_id=data.get("plan_id", ""),
            goal=data.get("goal", ""),
            current_step_idx=data.get("current_step_idx", 0),
            status=data.get("status", "active"),
            created_at=data.get("created_at", 0),
            completed_at=data.get("completed_at", 0),
            total_steps_executed=data.get("total_steps_executed", 0),
            total_distance_cm=data.get("total_distance_cm", 0),
            findings=data.get("findings", []),
        )
        plan.steps = [PlanStep.from_dict(s) for s in data.get("steps", [])]
        return plan


# ── Plan Parser ──────────────────────────────────────────────────────

DIRECTION_KEYWORDS = {
    "forward": "forward", "ahead": "forward", "straight": "forward",
    "left": "left", "right": "right",
    "backward": "backward", "back": "backward", "reverse": "backward",
    "around": "turn_around",
}

SCENE_TYPE_KEYWORDS = {
    "kitchen": "kitchen", "hallway": "hallway", "hall": "hallway",
    "corridor": "hallway", "bedroom": "bedroom", "bathroom": "bathroom",
    "living room": "living_room", "garage": "garage",
    "patio": "patio", "yard": "yard", "office": "office",
    "stairs": "stairs", "staircase": "stairs",
    "doorway": "doorway", "door": "doorway",
    "outside": "outdoor", "outdoor": "outdoor",
}


def parse_natural_language_plan(text: str, plan_id: str = "") -> NavPlan:
    """Parse a natural-language exploration plan into structured steps.

    Handles inputs like:
        "Go down the hall, turn right, check the room on the left"
        "Explore the kitchen, then go to the living room"
        "Go forward 100 steps, turn left, go forward 50 steps"

    Splits on commas, "then", "and then", periods, semicolons.
    Each segment becomes a PlanStep with inferred action + completion.
    """
    if not plan_id:
        plan_id = f"plan_{int(time.time()) % 100000}"

    # Split into segments
    segments = re.split(r'[,;.]|\bthen\b|\band then\b', text, flags=re.IGNORECASE)
    segments = [s.strip() for s in segments if s.strip()]

    steps: List[PlanStep] = []
    for i, seg in enumerate(segments):
        step = _parse_segment(seg, step_id=i)
        if step:
            steps.append(step)

    if not steps:
        steps.append(PlanStep(
            step_id=0,
            action="go_forward",
            description=text[:100],
            direction="forward",
            completion=CompletionType.STEPS_TAKEN,
            completion_value=30,
            max_steps=50,
        ))

    plan = NavPlan(
        plan_id=plan_id,
        goal=text[:200],
        steps=steps,
        created_at=time.time(),
    )

    if steps:
        steps[0].status = StepStatus.ACTIVE
        steps[0].started_at = time.time()

    return plan


def _parse_segment(text: str, step_id: int) -> Optional[PlanStep]:
    """Parse a single plan segment into a PlanStep."""
    lower = text.lower().strip()
    if not lower or len(lower) < 3:
        return None

    # Extract numbers (for step counts or distances)
    numbers = re.findall(r'\d+', lower)
    num_value = int(numbers[0]) if numbers else 20

    # Check for "look around" / "scan" / "observe"
    if any(w in lower for w in ("look around", "scan", "observe", "check surroundings")):
        return PlanStep(
            step_id=step_id,
            action="look_around",
            description=text[:100],
            direction="forward",
            completion=CompletionType.STEPS_TAKEN,
            completion_value=5,
            max_steps=10,
        )

    # Check for goto room type: "go to the kitchen"
    for keyword, scene_type in SCENE_TYPE_KEYWORDS.items():
        if keyword in lower:
            is_goto = any(w in lower for w in ("go to", "goto", "head to",
                                                "navigate to", "find the",
                                                "explore the", "check the"))
            if is_goto or keyword == lower:
                return PlanStep(
                    step_id=step_id,
                    action="goto_room",
                    description=text[:100],
                    target=scene_type,
                    completion=CompletionType.SCENE_TYPE,
                    completion_value=scene_type,
                    max_steps=max(num_value, 100),
                    note=f"Looking for {scene_type}",
                )

    # Check for directional movement: "go left", "turn right", "forward 50 steps"
    for keyword, direction in DIRECTION_KEYWORDS.items():
        if keyword in lower:
            has_distance = "cm" in lower or "meter" in lower
            if has_distance:
                comp = CompletionType.DISTANCE_CM
                val = num_value * (100 if "meter" in lower else 1)
            else:
                comp = CompletionType.STEPS_TAKEN
                val = num_value if numbers else 20

            is_turn = any(w in lower for w in ("turn", "rotate", "spin"))
            if is_turn and direction in ("left", "right"):
                return PlanStep(
                    step_id=step_id,
                    action=f"go_{direction}",
                    description=text[:100],
                    direction=f"turn_{direction}",
                    completion=CompletionType.STEPS_TAKEN,
                    completion_value=3,
                    max_steps=5,
                )

            return PlanStep(
                step_id=step_id,
                action=f"go_{direction}",
                description=text[:100],
                direction=direction,
                completion=comp,
                completion_value=val,
                max_steps=max(val + 10, 50),
            )

    # Default: forward movement
    return PlanStep(
        step_id=step_id,
        action="go_forward",
        description=text[:100],
        direction="forward",
        completion=CompletionType.STEPS_TAKEN,
        completion_value=num_value if numbers else 20,
        max_steps=max(num_value + 10 if numbers else 30, 30),
    )


# ── Plan Executor ────────────────────────────────────────────────────

class NavPlanExecutor:
    """Monitors and advances multi-step exploration plans.

    Called each explorer step to check completion conditions and
    advance to the next action. Interfaces with the explorer's
    conscious intent system to steer the body.
    """

    def __init__(self):
        self._active_plan: Optional[NavPlan] = None
        self._prev_x: float = 0.0
        self._prev_y: float = 0.0
        self._load()

    def _load(self):
        """Load active plan from disk."""
        if PLAN_FILE.exists():
            try:
                data = json.loads(PLAN_FILE.read_text())
                if data.get("status") == "active":
                    self._active_plan = NavPlan.from_dict(data)
                    logger.info(
                        f"Loaded nav plan: {self._active_plan.goal[:60]} "
                        f"({self._active_plan.current_step_idx + 1}/"
                        f"{len(self._active_plan.steps)} steps)")
            except Exception as e:
                logger.debug(f"Failed to load nav plan: {e}")

    def save(self):
        """Persist active plan to disk."""
        try:
            PLAN_FILE.parent.mkdir(parents=True, exist_ok=True)
            if self._active_plan:
                PLAN_FILE.write_text(
                    json.dumps(self._active_plan.to_dict(), indent=2))
            elif PLAN_FILE.exists():
                PLAN_FILE.unlink()
        except Exception as e:
            logger.debug(f"Failed to save nav plan: {e}")

    @property
    def active(self) -> bool:
        return (self._active_plan is not None
                and self._active_plan.status == "active")

    @property
    def plan(self) -> Optional[NavPlan]:
        return self._active_plan

    def set_plan(self, plan: NavPlan):
        """Set a new active plan, replacing any existing one."""
        if self._active_plan and self._active_plan.status == "active":
            self._active_plan.cancel("replaced by new plan")
        self._active_plan = plan
        self.save()
        logger.info(
            f"Nav plan set: {plan.goal[:60]} ({len(plan.steps)} steps)")

    def cancel(self, reason: str = "cancelled by user") -> Dict[str, Any]:
        """Cancel the active plan."""
        if not self._active_plan:
            return {"status": "no_plan"}
        self._active_plan.cancel(reason)
        result = self._active_plan.to_dict()
        self.save()
        self._active_plan = None
        return result

    def tick(self, robot_x: float, robot_y: float,
             scene_type: str = "",
             scene_desc: str = "") -> Optional[Dict[str, str]]:
        """Called each explorer step. Checks completion, advances plan.

        Returns an intent dict {"direction": ..., "reason": ...} if the
        plan wants to steer, or None if no active plan / current step
        doesn't need steering.
        """
        if not self.active:
            return None

        plan = self._active_plan
        step = plan.current_step()
        if not step:
            plan.status = "completed"
            plan.completed_at = time.time()
            self.save()
            return None

        # Update step metrics
        dx = robot_x - self._prev_x
        dy = robot_y - self._prev_y
        moved = (dx * dx + dy * dy) ** 0.5
        step.steps_executed += 1
        step.distance_traveled += moved
        plan.total_steps_executed += 1
        plan.total_distance_cm += moved
        self._prev_x = robot_x
        self._prev_y = robot_y

        # Check completion conditions
        completed = False
        reason = ""

        if step.completion == CompletionType.STEPS_TAKEN:
            if step.steps_executed >= step.completion_value:
                completed = True
                reason = f"{step.steps_executed} steps taken"

        elif step.completion == CompletionType.DISTANCE_CM:
            if step.distance_traveled >= step.completion_value:
                completed = True
                reason = f"{step.distance_traveled:.0f}cm traveled"

        elif step.completion == CompletionType.SCENE_TYPE:
            if scene_type and scene_type == step.completion_value:
                completed = True
                reason = f"reached {scene_type}"
                plan.findings.append(f"Found {scene_type} at step {step.step_id + 1}")

        elif step.completion == CompletionType.LANDMARK_FOUND:
            if (step.completion_value
                    and scene_desc
                    and step.completion_value.lower() in scene_desc.lower()):
                completed = True
                reason = f"found: {step.completion_value}"
                plan.findings.append(f"Found {step.completion_value}")

        # Timeout check
        if not completed and step.steps_executed >= step.max_steps:
            completed = True
            reason = f"timeout after {step.max_steps} steps"

        if completed:
            logger.info(
                f"Nav plan step {step.step_id + 1} complete: "
                f"{step.description[:40]} ({reason})")
            next_step = plan.advance(reason)
            if next_step:
                self._apply_step_intent(next_step)
            self.save()
            if next_step:
                return {
                    "direction": next_step.direction,
                    "reason": f"Plan step {next_step.step_id + 1}: "
                              f"{next_step.description[:50]}",
                }
            return None

        # Return steering for current step
        return self._step_to_intent(step)

    def _apply_step_intent(self, step: PlanStep):
        """Set explorer intent from a plan step."""
        try:
            from repryntt.hardware.explorer import get_explorer
            explorer = get_explorer()
            intent = self._step_to_intent(step)
            if intent:
                explorer.set_intent(
                    direction=intent["direction"],
                    reason=intent["reason"],
                    duration_steps=step.max_steps,
                )
        except Exception as e:
            logger.debug(f"Failed to apply plan step intent: {e}")

    def _step_to_intent(self, step: PlanStep) -> Optional[Dict[str, str]]:
        """Convert a plan step to a direction intent."""
        if step.action == "look_around":
            return None

        if step.action.startswith("goto_"):
            # For goto actions, use path planning to determine direction
            try:
                from repryntt.hardware.spatial_map import get_spatial_map
                smap = get_spatial_map()

                if step.action == "goto_room":
                    room = smap.find_room_by_type(step.target)
                    if room:
                        path = smap.plan_path_to_room(room.room_id)
                        if path.success:
                            return {
                                "direction": path.direction_name,
                                "reason": f"Heading to {step.target} "
                                          f"({path.distance_cm:.0f}cm)",
                            }
                elif step.action == "goto_coords" and "," in step.target:
                    parts = step.target.split(",")
                    gx, gy = float(parts[0]), float(parts[1])
                    path = smap.plan_path_to(gx, gy)
                    if path.success:
                        return {
                            "direction": path.direction_name,
                            "reason": f"Heading to ({gx:.0f},{gy:.0f}) "
                                      f"({path.distance_cm:.0f}cm)",
                        }
            except Exception:
                pass

        direction = step.direction
        if direction.startswith("turn_"):
            direction = direction[5:]

        return {
            "direction": direction,
            "reason": f"Plan: {step.description[:50]}",
        }

    def get_context(self) -> Optional[str]:
        """Get plan status for Andrew's heartbeat context."""
        if not self.active:
            return None
        plan = self._active_plan
        step = plan.current_step()
        if not step:
            return None

        lines = [
            "## ACTIVE NAVIGATION PLAN",
            plan.progress_summary(),
            "",
            "Upcoming steps:",
        ]
        for s in plan.steps[plan.current_step_idx + 1:
                            plan.current_step_idx + 4]:
            lines.append(f"  {s.step_id + 1}. {s.description}")

        return "\n".join(lines)


# ── Singleton ────────────────────────────────────────────────────────

_executor: Optional[NavPlanExecutor] = None


def get_nav_planner() -> NavPlanExecutor:
    """Get or create the singleton nav plan executor."""
    global _executor
    if _executor is None:
        _executor = NavPlanExecutor()
    return _executor
