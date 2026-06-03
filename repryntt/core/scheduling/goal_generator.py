"""
GoalGenerator — Drive + Role Based Goal Production
===================================================

Generates daily goals by combining:
1. Drive state (which drive is dominant → goal category)
2. Relational mode weights (which role context is active → goal filtering)
3. External context (pending comms, marketplace, calendar)
4. Long-term threads (weekly deep-think outputs)

Replaces flat daily plan generation with structured, role-tagged, priority-scored goals.
Called by JarvisLearning.get_daily_plan() when enabled.

Uses existing infrastructure:
- WorldState for current drive/mode/emotional state
- Value Compass for alignment scoring (from consciousness.py)
- Pursuit system for long-term threads
- real_web_search / knowledge_router for grounding (weekly deep-think only)
- existing task_queue.seed_from_daily_plan() interface
"""

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from repryntt.core.awareness.world_state import WorldState, get_world_state

logger = logging.getLogger("repryntt.scheduling.goal_generator")

# Drive → goal category mapping
DRIVE_GOAL_CATEGORIES = {
    "civilization": ["community", "marketplace", "collaboration", "open_source"],
    "guardian": ["security", "monitoring", "backup", "safety_check"],
    "understanding": ["research", "learning", "reading", "experimentation"],
    "evolution": ["self_improvement", "training", "code_refactor", "skill_building"],
    "consciousness": ["introspection", "journaling", "meditation", "value_alignment"],
}

# Relational mode → goal type affinity (higher = more relevant to this mode)
MODE_GOAL_AFFINITY = {
    "self": {"research": 0.9, "learning": 0.9, "self_improvement": 0.9,
             "introspection": 0.8, "experimentation": 0.8, "skill_building": 0.7,
             "code_refactor": 0.6, "journaling": 0.7},
    "employee": {"marketplace": 0.9, "collaboration": 0.7, "code_refactor": 0.6,
                 "monitoring": 0.5, "backup": 0.5, "security": 0.6},
    "friend": {"community": 0.9, "collaboration": 0.8, "open_source": 0.8,
               "learning": 0.5, "research": 0.5},
    "family": {"monitoring": 0.6, "backup": 0.7, "safety_check": 0.8,
               "journaling": 0.5, "introspection": 0.4},
}

GOALS_PER_DAY = 5
DEEP_THINK_DIR = "deep_think"


@dataclass
class Goal:
    """A single generated goal with metadata."""
    title: str
    category: str
    priority: float  # 0.0 - 1.0
    mode_affinity: str  # which relational mode this aligns with most
    drive_source: str  # which drive generated this
    description: str = ""
    estimated_duration_min: int = 30
    tags: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def to_task_queue_format(self) -> Dict[str, Any]:
        """Format for existing TaskQueue.add_task() interface."""
        return {
            "prompt": self.title,
            "priority": "daily_plan",
            "source": f"goal_generator:{self.drive_source}",
            "metadata": {
                "category": self.category,
                "mode_affinity": self.mode_affinity,
                "priority_score": self.priority,
                "drive_source": self.drive_source,
                "tags": self.tags,
            },
        }


class GoalGenerator:
    """Produces daily goals from system state.

    Core method: generate_daily_goals() → List[Goal]
    """

    def __init__(self, world_state: Optional[WorldState] = None,
                 data_dir: Optional[Path] = None):
        self._world_state = world_state or get_world_state()
        self._data_dir = data_dir or (Path.home() / ".repryntt" / "brain")
        self._deep_think_dir = self._data_dir / DEEP_THINK_DIR
        self._deep_think_dir.mkdir(parents=True, exist_ok=True)

    def generate_daily_goals(self,
                             drive_levels: Optional[Dict[str, float]] = None,
                             mode_weights: Optional[Dict[str, float]] = None,
                             pending_context: Optional[Dict[str, Any]] = None,
                             ) -> List[Goal]:
        """Generate prioritized goals for today.

        Args:
            drive_levels: Current drive levels (from WorldState or direct)
            mode_weights: Current relational mode weights (from WorldState or direct)
            pending_context: External context (pending messages, jobs, etc.)

        Returns:
            Sorted list of Goals (highest priority first)
        """
        # Read from WorldState if not provided directly
        if drive_levels is None:
            drive_levels = self._world_state.drive_state.drive_levels
        if mode_weights is None:
            mode_weights = self._world_state.mode_weights.weights

        if not drive_levels:
            drive_levels = {"understanding": 0.5, "evolution": 0.4,
                           "civilization": 0.3, "guardian": 0.2, "consciousness": 0.2}
        if not mode_weights:
            mode_weights = {"self": 0.4, "employee": 0.25, "friend": 0.2, "family": 0.15}

        pending = pending_context or {}

        goals = []

        # Generate goals from each drive, weighted by drive level
        for drive, level in sorted(drive_levels.items(), key=lambda x: -x[1]):
            if level < 0.15:
                continue
            categories = DRIVE_GOAL_CATEGORIES.get(drive, [])
            for category in categories[:2]:  # Top 2 categories per drive
                goal = self._create_goal(
                    category=category,
                    drive=drive,
                    drive_level=level,
                    mode_weights=mode_weights,
                    pending=pending,
                )
                if goal:
                    goals.append(goal)

        # Add context-driven goals (pending messages, marketplace jobs)
        context_goals = self._goals_from_context(pending, mode_weights)
        goals.extend(context_goals)

        # Add deep-think thread goals (if any exist)
        thread_goals = self._goals_from_deep_think(mode_weights)
        goals.extend(thread_goals)

        # Score and sort
        for goal in goals:
            goal.priority = self._compute_priority(goal, drive_levels, mode_weights)

        goals.sort(key=lambda g: -g.priority)
        return goals[:GOALS_PER_DAY]

    def _create_goal(self, category: str, drive: str, drive_level: float,
                     mode_weights: Dict[str, float], pending: Dict) -> Optional[Goal]:
        """Create a single goal for a category/drive combination."""
        # Find best mode affinity for this category
        best_mode = "self"
        best_affinity = 0.0
        for mode, affinities in MODE_GOAL_AFFINITY.items():
            affinity = affinities.get(category, 0.0)
            if affinity > best_affinity:
                best_affinity = affinity
                best_mode = mode

        title = self._generate_title(category, drive)
        return Goal(
            title=title,
            category=category,
            priority=drive_level * best_affinity,
            mode_affinity=best_mode,
            drive_source=drive,
            tags=[drive, category, best_mode],
        )

    def _generate_title(self, category: str, drive: str) -> str:
        """Generate a human-readable goal title."""
        templates = {
            "research": f"Research topic aligned with {drive} drive",
            "learning": f"Learn something new ({drive}-motivated)",
            "self_improvement": "Work on self-improvement and skill refinement",
            "community": "Engage with community and contribute",
            "marketplace": "Check marketplace for compute/service opportunities",
            "collaboration": "Seek collaboration opportunities",
            "open_source": "Contribute to open source or public knowledge",
            "security": "Security review and system hardening",
            "monitoring": "System health monitoring and maintenance",
            "backup": "Verify backups and data integrity",
            "safety_check": "Run safety and alignment checks",
            "training": "Self-evolution training cycle",
            "code_refactor": "Code quality improvement and refactoring",
            "skill_building": "Build new capability or skill package",
            "introspection": "Introspective journaling and value reflection",
            "journaling": "Update personal journal with recent learnings",
            "meditation": "Quiet contemplation and state assessment",
            "value_alignment": "Review decisions against value compass",
            "experimentation": "Run a new experiment or test hypothesis",
        }
        return templates.get(category, f"{category.replace('_', ' ').title()} ({drive})")

    def _goals_from_context(self, pending: Dict, mode_weights: Dict) -> List[Goal]:
        """Generate goals from external context (messages, jobs, etc.)."""
        goals = []
        if pending.get("unread_messages", 0) > 0:
            goals.append(Goal(
                title=f"Respond to {pending['unread_messages']} pending messages",
                category="community",
                priority=0.7,
                mode_affinity="friend",
                drive_source="civilization",
                tags=["comms", "friend", "civilization"],
            ))
        if pending.get("marketplace_jobs", 0) > 0:
            goals.append(Goal(
                title=f"Evaluate {pending['marketplace_jobs']} marketplace opportunities",
                category="marketplace",
                priority=0.6,
                mode_affinity="employee",
                drive_source="civilization",
                tags=["marketplace", "employee", "civilization"],
            ))
        return goals

    def _goals_from_deep_think(self, mode_weights: Dict) -> List[Goal]:
        """Pull goals from weekly deep-think thread files."""
        goals = []
        try:
            thread_files = sorted(self._deep_think_dir.glob("*.json"), reverse=True)
            if not thread_files:
                return goals

            latest = thread_files[0]
            data = json.loads(latest.read_text())
            threads = data.get("threads", [])

            for thread in threads[:2]:
                goals.append(Goal(
                    title=f"Explore: {thread.get('question', 'deep question')}",
                    category="research",
                    priority=0.5,
                    mode_affinity="self",
                    drive_source="consciousness",
                    description=thread.get("direction", ""),
                    tags=["deep_think", "self", "consciousness"],
                ))
        except Exception as e:
            logger.debug("Failed to load deep-think threads: %s", e)

        return goals

    def _compute_priority(self, goal: Goal, drive_levels: Dict, mode_weights: Dict) -> float:
        """Final priority score combining drive level, mode affinity, and base priority."""
        drive_factor = drive_levels.get(goal.drive_source, 0.3)
        mode_factor = mode_weights.get(goal.mode_affinity, 0.25)

        # Weighted combination: base priority 40%, drive alignment 35%, mode alignment 25%
        return (
            0.40 * goal.priority +
            0.35 * drive_factor +
            0.25 * mode_factor
        )

    def format_as_daily_plan(self, goals: Optional[List[Goal]] = None) -> str:
        """Format goals as markdown daily plan (compatible with existing daily_plan format)."""
        if goals is None:
            goals = self.generate_daily_goals()

        lines = [f"# Daily Plan — Generated {time.strftime('%Y-%m-%d %H:%M')}", ""]
        lines.append(f"**Mode blend**: {self._format_mode_weights()}")
        lines.append(f"**Dominant drive**: {self._world_state.drive_state.dominant_drive}")
        lines.append("")

        for i, goal in enumerate(goals, 1):
            checkbox = f"- [ ] **{goal.title}**"
            meta = f"  - Priority: {goal.priority:.2f} | Mode: {goal.mode_affinity} | Drive: {goal.drive_source}"
            lines.append(checkbox)
            lines.append(meta)
            if goal.description:
                lines.append(f"  - {goal.description}")
            lines.append("")

        return "\n".join(lines)

    def _format_mode_weights(self) -> str:
        weights = self._world_state.mode_weights.weights
        if not weights:
            return "Self 50% / Employee 20% / Friend 15% / Family 15%"
        sorted_modes = sorted(weights.items(), key=lambda x: -x[1])
        return " / ".join(f"{m.title()} {int(w*100)}%" for m, w in sorted_modes if w > 0.05)


_singleton: Optional[GoalGenerator] = None


def get_goal_generator(world_state: Optional[WorldState] = None) -> GoalGenerator:
    """Singleton accessor."""
    global _singleton
    if _singleton is None:
        _singleton = GoalGenerator(world_state)
    return _singleton
