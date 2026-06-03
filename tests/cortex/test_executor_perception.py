"""Tests for executor and perception cortex regions."""

import pytest
from unittest.mock import patch, MagicMock
from repryntt.cortex.regions.executor import ExecutorRegion, ACTION_VOCABULARY, PROCESS_TYPES as EXEC_TYPES
from repryntt.cortex.regions.perception import PerceptionRegion, PROCESS_TYPES as PERC_TYPES


# ═══════════════════════════════════════════════════════════════════════
#  Executor Tests
# ═══════════════════════════════════════════════════════════════════════

class TestExecutorActionVocabulary:
    """Tests for the ACTION_VOCABULARY and process types."""

    def test_stop_has_zero_velocities(self):
        assert ACTION_VOCABULARY["stop"]["linear"] == 0.0
        assert ACTION_VOCABULARY["stop"]["angular"] == 0.0

    def test_forward_actions_positive_linear(self):
        assert ACTION_VOCABULARY["forward_slow"]["linear"] > 0
        assert ACTION_VOCABULARY["forward_normal"]["linear"] > 0

    def test_backward_actions_negative_linear(self):
        assert ACTION_VOCABULARY["backward_slow"]["linear"] < 0

    def test_turn_actions_have_angular(self):
        assert ACTION_VOCABULARY["turn_left"]["angular"] != 0
        assert ACTION_VOCABULARY["turn_right"]["angular"] != 0

    def test_all_actions_have_duration(self):
        for name, params in ACTION_VOCABULARY.items():
            assert "duration" in params, f"{name} missing duration"

    def test_process_types_complete(self):
        assert "select_action" in EXEC_TYPES
        assert "plan_trajectory" in EXEC_TYPES
        assert "reflex" in EXEC_TYPES
        assert "validate_command" in EXEC_TYPES


class TestExecutorRuleSelect:
    """Tests for rule-based action selection."""

    @pytest.fixture
    def executor(self):
        e = ExecutorRegion()
        e._ros2_available = True
        e._model_name = None
        return e

    def test_obstacle_close_returns_stop(self, executor):
        result = executor._rule_select("go forward", {"obstacle_distance_m": 0.2})
        assert result == "stop"

    def test_obstacle_medium_returns_retreat(self, executor):
        result = executor._rule_select("go forward", {"obstacle_distance_m": 0.4})
        assert result == "retreat"

    def test_forward_keyword(self, executor):
        assert executor._rule_select("move forward", {}) == "forward_normal"

    def test_backward_keyword(self, executor):
        assert executor._rule_select("go back", {}) == "retreat"

    def test_turn_left_keyword(self, executor):
        assert executor._rule_select("turn left please", {}) == "turn_left"

    def test_turn_right_keyword(self, executor):
        assert executor._rule_select("turn right", {}) == "turn_right"

    def test_stop_keyword(self, executor):
        assert executor._rule_select("stop now", {}) == "stop"

    def test_approach_keyword(self, executor):
        assert executor._rule_select("approach the table", {}) == "approach"

    def test_rotate_keyword(self, executor):
        assert executor._rule_select("turn around", {}) == "rotate_180"

    def test_unknown_defaults_to_stop(self, executor):
        assert executor._rule_select("think about philosophy", {}) == "stop"


class TestExecutorReflex:
    """Tests for reflex arcs."""

    @pytest.fixture
    def executor(self):
        e = ExecutorRegion()
        e._ros2_available = True
        return e

    def test_obstacle_very_close_urgent_stop(self, executor):
        result = executor._reflex({
            "trigger": "obstacle",
            "sensor_data": {"distance_m": 0.1},
        })
        assert result["result"]["action"] == "stop"
        assert result["result"]["urgent"] is True

    def test_obstacle_medium_retreat(self, executor):
        result = executor._reflex({
            "trigger": "obstacle",
            "sensor_data": {"distance_m": 0.4},
        })
        assert result["result"]["action"] == "retreat"

    def test_cliff_always_stops(self, executor):
        result = executor._reflex({"trigger": "cliff", "sensor_data": {}})
        assert result["result"]["action"] == "stop"
        assert result["result"]["urgent"] is True

    def test_bump_retreats(self, executor):
        result = executor._reflex({"trigger": "bump", "sensor_data": {}})
        assert result["result"]["action"] == "retreat"

    def test_unknown_trigger_stops(self, executor):
        result = executor._reflex({"trigger": "unknown", "sensor_data": {}})
        assert result["result"]["action"] == "stop"


class TestExecutorValidation:
    """Tests for command validation."""

    @pytest.fixture
    def executor(self):
        e = ExecutorRegion()
        e._ros2_available = True
        return e

    def test_safe_command_valid(self, executor):
        result = executor._validate_command({
            "linear_velocity": 0.3,
            "angular_velocity": 0.5,
            "duration": 2.0,
        })
        assert result["result"]["valid"] is True
        assert result["result"]["issues"] == []

    def test_excessive_linear_rejected(self, executor):
        result = executor._validate_command({
            "linear_velocity": 1.0,
            "angular_velocity": 0.0,
            "duration": 1.0,
        })
        assert result["result"]["valid"] is False
        assert any("Linear" in i for i in result["result"]["issues"])

    def test_excessive_angular_rejected(self, executor):
        result = executor._validate_command({
            "linear_velocity": 0.0,
            "angular_velocity": 2.0,
            "duration": 1.0,
        })
        assert result["result"]["valid"] is False
        assert any("Angular" in i for i in result["result"]["issues"])

    def test_excessive_duration_rejected(self, executor):
        result = executor._validate_command({
            "linear_velocity": 0.1,
            "angular_velocity": 0.0,
            "duration": 15.0,
        })
        assert result["result"]["valid"] is False
        assert any("Duration" in i for i in result["result"]["issues"])

    def test_multiple_violations_all_reported(self, executor):
        result = executor._validate_command({
            "linear_velocity": 1.0,
            "angular_velocity": 2.0,
            "duration": 15.0,
        })
        assert len(result["result"]["issues"]) == 3


class TestExecutorTrajectory:
    """Tests for trajectory planning."""

    @pytest.fixture
    def executor(self):
        e = ExecutorRegion()
        e._ros2_available = True
        e._model_name = None
        return e

    def test_waypoints_produce_trajectory(self, executor):
        result = executor._plan_trajectory({
            "goal": "go to kitchen",
            "waypoints": [
                {"rotate_deg": 90},
                {"rotate_deg": 0},
            ],
        })
        assert result["success"] is True
        # 2 waypoints: first has rotate (turn + forward), second has no rotate (just forward)
        assert result["result"]["step_count"] >= 2

    def test_no_waypoints_single_action(self, executor):
        result = executor._plan_trajectory({"goal": "go forward", "waypoints": []})
        assert result["result"]["step_count"] == 1

    def test_ros2_unavailable_rejects_all(self):
        e = ExecutorRegion()
        e._ros2_available = False
        result = e.process({"type": "select_action", "goal": "forward"})
        assert result["success"] is False
        assert "ROS2" in result["error"]


class TestExecutorProcessRouting:
    """Tests for process() dispatch."""

    @pytest.fixture
    def executor(self):
        e = ExecutorRegion()
        e._ros2_available = True
        e._model_name = None
        return e

    def test_select_action_routes(self, executor):
        result = executor.process({"type": "select_action", "goal": "go forward"})
        assert result["success"] is True
        assert result["result"]["action"] == "forward_normal"

    def test_unknown_type_errors(self, executor):
        result = executor.process({"type": "dance"})
        assert result["success"] is False
        assert "Unknown" in result["error"]


# ═══════════════════════════════════════════════════════════════════════
#  Perception Tests
# ═══════════════════════════════════════════════════════════════════════

class TestPerceptionProcessTypes:
    """Tests for perception PROCESS_TYPES."""

    def test_all_types_registered(self):
        assert "classify_image" in PERC_TYPES
        assert "detect_audio_event" in PERC_TYPES
        assert "fuse_sensors" in PERC_TYPES
        assert "detect_anomaly" in PERC_TYPES
        assert "describe_scene" in PERC_TYPES


class TestPerceptionSensorFusion:
    """Tests for sensor fusion."""

    @pytest.fixture
    def perception(self):
        p = PerceptionRegion()
        p._camera_available = True
        p._mic_available = True
        return p

    def test_fuse_returns_environment_state(self, perception):
        result = perception._fuse_sensors({
            "camera": {"people_count": 2},
            "audio": {"energy": 0.03},
            "distance_sensors": {"min_distance_m": 0.8},
        })
        assert result["success"] is True
        state = result["result"]["environment_state"]
        assert state["people_detected"] == 2
        assert state["obstacle_near"] is False
        assert state["noise_level"] == 0.03

    def test_fuse_detects_obstacle(self, perception):
        result = perception._fuse_sensors({
            "camera": {},
            "audio": {},
            "distance_sensors": {"min_distance_m": 0.3},
        })
        assert result["result"]["environment_state"]["obstacle_near"] is True

    def test_fuse_logs_to_perception_log(self, perception):
        perception._fuse_sensors({"camera": {}, "audio": {}, "distance_sensors": {}})
        assert len(perception._perception_log) == 1
        perception._fuse_sensors({"camera": {}, "audio": {}, "distance_sensors": {}})
        assert len(perception._perception_log) == 2


class TestPerceptionAnomalyDetection:
    """Tests for anomaly detection."""

    @pytest.fixture
    def perception(self):
        p = PerceptionRegion()
        return p

    def test_first_observation_no_anomaly(self, perception):
        result = perception._detect_anomaly({
            "current_state": {"brightness": 100, "people": 0},
        })
        assert result["result"]["anomaly_score"] == 0.0
        assert result["result"]["is_first"] is True

    def test_identical_state_no_anomaly(self, perception):
        state = {"brightness": 100, "people": 0}
        perception._detect_anomaly({"current_state": state})
        result = perception._detect_anomaly({"current_state": state})
        assert result["result"]["anomaly_score"] == 0.0
        assert result["result"]["is_anomaly"] is False

    def test_changed_state_high_anomaly(self, perception):
        perception._detect_anomaly({
            "current_state": {"brightness": 100, "people": 0},
        })
        result = perception._detect_anomaly({
            "current_state": {"brightness": 20, "people": 3},
        })
        # Both fields changed out of 2 → score = 1.0
        assert result["result"]["anomaly_score"] > 0.3
        assert result["result"]["is_anomaly"] is True


class TestPerceptionHeuristicClassify:
    """Tests for heuristic image classification."""

    def test_numpy_array_gets_brightness(self):
        import numpy as np
        frame = np.ones((100, 100, 3), dtype=np.uint8) * 128
        result = PerceptionRegion._heuristic_classify(frame)
        assert result["success"] is True
        assert result["result"]["brightness"] == 128.0
        assert result["result"]["model_based"] is False

    def test_non_array_fallback(self):
        result = PerceptionRegion._heuristic_classify("not_an_array")
        assert result["success"] is True
        assert result.get("fallback") is True


class TestPerceptionAudioEvent:
    """Tests for audio event detection."""

    @pytest.fixture
    def perception(self):
        p = PerceptionRegion()
        return p

    def test_no_audio_returns_error(self, perception):
        result = perception._detect_audio_event({"audio": None})
        assert result["success"] is False

    def test_quiet_audio_not_speech(self, perception):
        import numpy as np
        audio = np.zeros(16000, dtype=np.float32)
        result = perception._detect_audio_event({"audio": audio})
        assert result["result"]["is_speech_likely"] is False
        assert result["result"]["is_loud"] is False

    def test_loud_audio_detected(self, perception):
        import numpy as np
        audio = np.ones(16000, dtype=np.float32) * 0.5
        result = perception._detect_audio_event({"audio": audio})
        assert result["result"]["is_speech_likely"] is True
        assert result["result"]["is_loud"] is True


class TestPerceptionRouting:
    """Tests for process() dispatch."""

    @pytest.fixture
    def perception(self):
        p = PerceptionRegion()
        p._camera_available = True
        p._mic_available = True
        return p

    def test_fuse_sensors_routes(self, perception):
        result = perception.process({
            "type": "fuse_sensors",
            "camera": {},
            "audio": {},
            "distance_sensors": {},
        })
        assert result["success"] is True

    def test_unknown_type_errors(self, perception):
        result = perception.process({"type": "fly"})
        assert result["success"] is False
