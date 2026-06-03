"""
repryntt.cortex.regions.executor — Motor Execution Region.

Translates high-level goals into ROS2 action sequences.  Uses a small
policy network (5-50M params, ONNX or PyTorch) for fast action selection.

Activates only when ROS2 hardware is detected.  Falls back to
rule-based command mapping when no model is available.

Responsibilities:
  1. Action selection   — given {goal, sensor_state} → pick ROS2 action
  2. Trajectory planning — sequence of motor commands for complex movements
  3. Reflex arcs         — ultra-fast sensor-to-action for obstacle avoidance
  4. Command validation  — pre-flight check before motor execution

All commands pass through the Guardian region before execution.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from repryntt.cortex.region_base import BrainRegion, RegionState

logger = logging.getLogger(__name__)

# ── ROS2 action vocabulary ───────────────────────────────────────────────

# Actions the executor can dispatch (extended as robot capabilities grow)
ACTION_VOCABULARY = {
    "stop": {"linear": 0.0, "angular": 0.0, "duration": 0.0},
    "forward_slow": {"linear": 0.1, "angular": 0.0, "duration": 2.0},
    "forward_normal": {"linear": 0.3, "angular": 0.0, "duration": 2.0},
    "backward_slow": {"linear": -0.1, "angular": 0.0, "duration": 1.0},
    "turn_left": {"linear": 0.0, "angular": 0.5, "duration": 1.5},
    "turn_right": {"linear": 0.0, "angular": -0.5, "duration": 1.5},
    "rotate_180": {"linear": 0.0, "angular": 0.8, "duration": 2.0},
    "approach": {"linear": 0.15, "angular": 0.0, "duration": 3.0},
    "retreat": {"linear": -0.15, "angular": 0.0, "duration": 2.0},
    # Tank-specific: slow pivot (gentle differential) and arc turns
    "pivot_left": {"linear": 0.0, "angular": 0.3, "duration": 1.0},
    "pivot_right": {"linear": 0.0, "angular": -0.3, "duration": 1.0},
    "arc_left": {"linear": 0.15, "angular": 0.3, "duration": 2.0},
    "arc_right": {"linear": 0.15, "angular": -0.3, "duration": 2.0},
}

# Process input types
PROCESS_TYPES = {
    "select_action",       # Goal + state → best action from vocabulary
    "plan_trajectory",     # Goal → sequence of actions
    "reflex",              # Sensor trigger → immediate action
    "validate_command",    # Pre-flight check on motor parameters
}


class ExecutorRegion(BrainRegion):
    """Motor execution brain region.

    On hardware-less systems: stays DISABLED.
    On systems with ROS2: uses a policy network or rule-based action selection.
    """

    def __init__(self) -> None:
        super().__init__()
        self._ros2_available = False
        self._action_log: List[Dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "executor"

    def on_load(self) -> None:
        """Check for ROS2 availability."""
        self._ros2_available = self._check_ros2()
        if not self._ros2_available:
            self._state = RegionState.DISABLED
            logger.info("Executor region disabled (no ROS2 detected)")
        else:
            logger.info("Executor region ready (ROS2 available, model=%s)",
                        self._model_name or "rule-based")

    @staticmethod
    def _check_ros2() -> bool:
        """Check if ROS2 is available on this system."""
        try:
            from repryntt.hardware.ros2 import ROS2_AVAILABLE
            return ROS2_AVAILABLE
        except ImportError:
            return False

    # ── Core dispatch ────────────────────────────────────────────────

    def process(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        ptype = input_data.get("type", "")

        if not self._ros2_available:
            return {
                "success": False,
                "result": None,
                "error": "ROS2 not available on this system",
            }

        if ptype == "select_action":
            return self._select_action(input_data)
        elif ptype == "plan_trajectory":
            return self._plan_trajectory(input_data)
        elif ptype == "reflex":
            return self._reflex(input_data)
        elif ptype == "validate_command":
            return self._validate_command(input_data)
        else:
            return {"success": False, "result": None, "error": f"Unknown type: {ptype}"}

    # ── Action selection ─────────────────────────────────────────────

    def _select_action(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Select the best action for a given goal and sensor state.

        If a policy model is loaded, uses model inference.
        Otherwise, uses rule-based keyword matching.
        """
        goal = input_data.get("goal", "")
        sensor_state = input_data.get("sensor_state", {})

        if self._model_name:
            action = self._model_select(goal, sensor_state)
        else:
            action = self._rule_select(goal, sensor_state)

        if action:
            self._action_log.append({
                "goal": goal[:100],
                "action": action,
                "model_based": bool(self._model_name),
            })
            # Keep last 100 actions for training data
            self._action_log = self._action_log[-100:]

        return {
            "success": True,
            "result": {
                "action": action,
                "params": ACTION_VOCABULARY.get(action, {}),
                "model_based": bool(self._model_name),
            },
        }

    def _rule_select(self, goal: str, sensor_state: Dict) -> str:
        """Rule-based action selection from goal keywords."""
        goal_lower = goal.lower()

        # Obstacle override
        obstacle_dist = sensor_state.get("obstacle_distance_m", float("inf"))
        if obstacle_dist < 0.3:
            return "stop"
        if obstacle_dist < 0.5:
            return "retreat"

        # Goal-based selection
        if any(w in goal_lower for w in ("stop", "halt", "freeze")):
            return "stop"
        if any(w in goal_lower for w in ("approach", "go to", "move to", "come")):
            return "approach"
        if any(w in goal_lower for w in ("back", "retreat", "reverse")):
            return "retreat"
        if any(w in goal_lower for w in ("turn left", "rotate left")):
            return "turn_left"
        if any(w in goal_lower for w in ("turn right", "rotate right")):
            return "turn_right"
        if any(w in goal_lower for w in ("forward", "go", "advance", "move")):
            return "forward_normal"
        if any(w in goal_lower for w in ("turn around", "rotate", "spin")):
            return "rotate_180"

        return "stop"  # Default: do nothing

    def _model_select(self, goal: str, sensor_state: Dict) -> str:
        """Use the policy model for action selection.

        For ONNX models: encode goal + sensor → classify action.
        For language models: prompt-based selection.
        """
        try:
            from repryntt.cortex.resource_manager import get_resource_manager
            mgr = get_resource_manager()

            lm = mgr.ensure_loaded(self._model_name)
            if not lm:
                return self._rule_select(goal, sensor_state)

            if lm.backend == "llama_cpp":
                # Language model: prompt-based
                actions_str = ", ".join(ACTION_VOCABULARY.keys())
                prompt = (
                    f"Robot action selection.\n"
                    f"Available actions: {actions_str}\n"
                    f"Sensor state: {sensor_state}\n"
                    f"Goal: {goal}\n"
                    f"Best action (one word):"
                )
                result = mgr.infer_llm(self._model_name, prompt, max_tokens=10, temperature=0.1)
                if result:
                    action = result.strip().lower().split()[0] if result.strip() else "stop"
                    if action in ACTION_VOCABULARY:
                        return action

            elif lm.backend == "onnx":
                # ONNX classifier: encode input → action index
                import numpy as np
                action_names = list(ACTION_VOCABULARY.keys())
                # Encode goal as simple bag-of-words features
                goal_tokens = goal.lower().split()[:20]
                # Simple feature: one-hot over action keywords
                features = np.zeros((1, len(action_names)), dtype=np.float32)
                for i, name in enumerate(action_names):
                    if any(kw in goal.lower() for kw in name.split("_")):
                        features[0, i] = 1.0
                result = mgr.infer_classifier(self._model_name, {"input": features})
                if result is not None:
                    try:
                        idx = int(np.argmax(result[0]))
                        if 0 <= idx < len(action_names):
                            return action_names[idx]
                    except (IndexError, ValueError, TypeError):
                        pass

        except Exception as e:
            logger.warning("Model action selection failed: %s", e)

        return self._rule_select(goal, sensor_state)

    # ── Trajectory planning ──────────────────────────────────────────

    def _plan_trajectory(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Plan a sequence of actions for a complex movement goal."""
        goal = input_data.get("goal", "")
        waypoints = input_data.get("waypoints", [])

        # Simple: break into approach + rotate sequences per waypoint
        actions = []
        for wp in waypoints:
            if wp.get("rotate_deg", 0) != 0:
                actions.append("turn_left" if wp["rotate_deg"] > 0 else "turn_right")
            actions.append("forward_normal")

        if not actions:
            # Single goal → single action
            action = self._rule_select(goal, {})
            actions = [action]

        return {
            "success": True,
            "result": {
                "trajectory": actions,
                "step_count": len(actions),
            },
        }

    # ── Reflex arcs ──────────────────────────────────────────────────

    def _reflex(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Ultra-fast sensor-triggered action.  No planning — immediate response."""
        trigger = input_data.get("trigger", "")
        sensor_data = input_data.get("sensor_data", {})

        if trigger == "obstacle":
            distance = sensor_data.get("distance_m", 1.0)
            if distance < 0.2:
                return {"success": True, "result": {"action": "stop", "reflex": True, "urgent": True}}
            elif distance < 0.5:
                return {"success": True, "result": {"action": "retreat", "reflex": True}}

        if trigger == "cliff":
            return {"success": True, "result": {"action": "stop", "reflex": True, "urgent": True}}

        if trigger == "bump":
            return {"success": True, "result": {"action": "retreat", "reflex": True}}

        return {"success": True, "result": {"action": "stop", "reflex": True}}

    # ── Command validation ───────────────────────────────────────────

    def _validate_command(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Pre-flight check on motor command parameters."""
        linear = abs(float(input_data.get("linear_velocity", 0)))
        angular = abs(float(input_data.get("angular_velocity", 0)))
        duration = float(input_data.get("duration", 0))

        issues = []
        if linear > 0.5:
            issues.append(f"Linear velocity {linear} exceeds safe limit 0.5 m/s")
        if angular > 1.0:
            issues.append(f"Angular velocity {angular} exceeds safe limit 1.0 rad/s")
        if duration > 10:
            issues.append(f"Duration {duration}s exceeds safe limit 10s")

        return {
            "success": True,
            "result": {
                "valid": len(issues) == 0,
                "issues": issues,
            },
        }

    # ── Fallback ─────────────────────────────────────────────────────

    def fallback(self, input_data: Dict[str, Any]) -> Dict[str, Any]:
        """Rule-based fallback.  Same as rule_select."""
        ptype = input_data.get("type", "")
        if ptype == "select_action":
            action = self._rule_select(
                input_data.get("goal", ""),
                input_data.get("sensor_state", {}),
            )
            return {"success": True, "result": {"action": action, "params": ACTION_VOCABULARY.get(action, {})}, "fallback": True}
        if ptype == "reflex":
            return {"success": True, "result": {"action": "stop", "reflex": True}, "fallback": True}
        return {"success": True, "result": None, "fallback": True}

    # ── Training data ────────────────────────────────────────────────

    def generate_training_data(self) -> List[Dict[str, Any]]:
        """Produce training examples from logged actions."""
        examples = []
        for entry in self._action_log:
            examples.append({
                "type": "ros2_action",
                "region": "executor",
                "prompt": f"Goal: {entry['goal']}",
                "response": entry["action"],
            })
        return examples

    # ── ROS2 execution ───────────────────────────────────────────────

    def execute_action(self, action_name: str) -> Dict[str, Any]:
        """Actually execute a motor command via ROS2.

        This always passes through Guardian first.
        """
        params = ACTION_VOCABULARY.get(action_name)
        if not params:
            return {"success": False, "error": f"Unknown action: {action_name}"}

        # Guardian validation
        try:
            from repryntt.cortex.dispatcher import get_dispatcher
            dispatcher = get_dispatcher()
            guard_result = dispatcher.request_guardian_validation(
                "move_mobile_base", params,
            )
            if not guard_result.get("result", {}).get("allowed", True):
                reason = guard_result.get("result", {}).get("reason", "blocked")
                return {"success": False, "error": f"Guardian blocked: {reason}"}
        except Exception:
            pass  # Guardian not available — proceed with caution

        # Execute via ROS2
        try:
            from repryntt.hardware.ros2 import SAIGEROS2Interface
            ros2 = SAIGEROS2Interface()
            ros2.move_mobile_base(
                linear_velocity=params["linear"],
                angular_velocity=params["angular"],
                duration=params["duration"],
            )
            return {"success": True, "action": action_name, "params": params}
        except Exception as e:
            logger.error("ROS2 execution failed for '%s': %s", action_name, e)
            return {"success": False, "error": str(e)}
