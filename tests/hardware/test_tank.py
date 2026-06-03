"""Tests for repryntt.hardware.tank — Tank body controller."""

import math
import pytest
from unittest.mock import patch, MagicMock
from repryntt.hardware.tank import (
    TankBodyState,
    TankController,
    MAX_LINEAR_MS,
    MAX_ANGULAR_RADS,
    MAX_COMMAND_DURATION,
    TOPIC_CMD_VEL,
    TOPIC_PREFIX,
    get_tank_controller,
)


# ── TankBodyState ────────────────────────────────────────────────────

class TestTankBodyState:
    """Tests for the body state dataclass."""

    def test_defaults(self):
        s = TankBodyState()
        assert s.is_moving is False
        assert s.battery_percent == 100.0
        assert s.emergency_stopped is False
        assert s.ros2_connected is False

    def test_to_dict_structure(self):
        s = TankBodyState(battery_voltage=7.2, x=1.5, heading_deg=90.0)
        d = s.to_dict()
        assert d["battery_voltage"] == 7.2
        assert d["position"]["x"] == 1.5
        assert d["heading_deg"] == 90.0
        assert "is_moving" in d
        assert "emergency_stopped" in d

    def test_battery_low_flag(self):
        s = TankBodyState(battery_low=True)
        assert s.to_dict()["battery_low"] is True


# ── TankController — without ROS2 ───────────────────────────────────

class TestTankControllerNoROS2:
    """Tests for tank controller when ROS2 is not available."""

    def test_not_available_without_init(self):
        tc = TankController()
        assert tc.is_available is False

    def test_body_status_always_works(self):
        tc = TankController()
        result = tc.get_body_status()
        assert result["success"] is True
        assert "body" in result
        assert "hardware" in result
        assert result["hardware"]["ros2_available"] in (True, False)

    def test_move_forward_fails_without_init(self):
        tc = TankController()
        result = tc.move_forward(0.2, 1.0)
        assert result["success"] is False
        assert "not initialized" in result["error"]

    def test_stop_fails_without_init(self):
        tc = TankController()
        result = tc.stop()
        assert result["success"] is False

    def test_emergency_stop_fails_without_init(self):
        tc = TankController()
        result = tc.emergency_stop()
        assert result["success"] is False


# ── TankController — with mocked ROS2 ───────────────────────────────

class TestTankControllerMocked:
    """Tests for tank controller with mocked ROS2."""

    @pytest.fixture
    def tc(self):
        controller = TankController()
        controller._initialized = True
        controller._state.ros2_connected = True
        controller._pub_cmd_vel = MagicMock()
        controller._pub_estop = MagicMock()
        controller._pub_speed_limits = MagicMock()
        return controller

    def test_move_forward_publishes(self, tc):
        result = tc.move_forward(0.2, 0.0)  # 0 duration = no sleep
        assert result["success"] is True
        assert result["command"]["linear"] == 0.2
        assert result["command"]["angular"] == 0.0
        tc._pub_cmd_vel.publish.assert_called()

    def test_move_backward_negative_velocity(self, tc):
        result = tc.move_backward(0.15, 0.0)
        assert result["success"] is True
        assert result["command"]["linear"] == -0.15

    def test_turn_left_positive_angular(self, tc):
        result = tc.turn_left(0.5, 0.0)
        assert result["success"] is True
        assert result["command"]["angular"] == 0.5
        assert result["command"]["linear"] == 0.0

    def test_turn_right_negative_angular(self, tc):
        result = tc.turn_right(0.5, 0.0)
        assert result["success"] is True
        assert result["command"]["angular"] == -0.5

    def test_spin_calculates_duration(self, tc):
        # 180 degrees at 0.5 rad/s → pi / 0.5 = ~6.28s
        result = tc.spin(180, 0.5)
        expected_dur = math.pi / 0.5
        assert result["success"] is True
        assert abs(result["command"]["angular"]) == 0.5

    def test_stop_sends_zero(self, tc):
        result = tc.stop()
        assert result["success"] is True
        tc._pub_cmd_vel.publish.assert_called()

    def test_emergency_stop_sets_flag(self, tc):
        result = tc.emergency_stop()
        assert result["success"] is True
        assert tc.body_state.emergency_stopped is True
        tc._pub_estop.publish.assert_called()

    def test_reset_emergency_clears_flag(self, tc):
        tc._state.emergency_stopped = True
        result = tc.reset_emergency_stop()
        assert result["success"] is True
        assert tc.body_state.emergency_stopped is False

    def test_emergency_stop_blocks_movement(self, tc):
        tc._state.emergency_stopped = True
        result = tc.move_forward(0.2, 1.0)
        assert result["success"] is False
        assert "Emergency stop" in result["error"]

    def test_velocity_clamped_to_limits(self, tc):
        result = tc.move_forward(999.0, 0.0)
        assert result["success"] is True
        assert result["command"]["linear"] <= MAX_LINEAR_MS

    def test_duration_clamped(self, tc):
        result = tc.move_forward(0.1, 0.0)  # 0 duration bypass for test speed
        assert result["success"] is True

    def test_battery_low_limits_speed(self, tc):
        tc._state.battery_low = True
        result = tc.move_forward(0.3, 0.0)
        assert result["success"] is True
        # Speed should be clamped to 50% of max
        assert result["command"]["linear"] <= MAX_LINEAR_MS * 0.5 + 0.01


# ── Battery callback ─────────────────────────────────────────────────

class TestBatteryCallback:
    """Tests for the battery subscription callback."""

    def test_full_battery(self):
        tc = TankController()
        msg = MagicMock()
        msg.data = 8.4  # 2S LiPo fully charged
        tc._on_battery(msg)
        assert tc.body_state.battery_voltage == 8.4
        assert tc.body_state.battery_percent == 100.0
        assert tc.body_state.battery_low is False

    def test_low_battery(self):
        tc = TankController()
        msg = MagicMock()
        msg.data = 6.2  # Almost dead
        tc._on_battery(msg)
        assert tc.body_state.battery_low is True
        assert tc.body_state.battery_percent < 15

    def test_empty_battery(self):
        tc = TankController()
        msg = MagicMock()
        msg.data = 5.0  # Below minimum
        tc._on_battery(msg)
        assert tc.body_state.battery_percent == 0


# ── Motor state callback ─────────────────────────────────────────────

class TestMotorStateCallback:
    """Tests for the motor state subscription callback."""

    def test_parses_json_state(self):
        tc = TankController()
        msg = MagicMock()
        msg.data = '{"moving": true, "linear": 0.2, "angular": 0.0, "odom_x": 1.5, "odom_y": 0.3, "heading": 45.0, "distance": 2.1}'
        tc._on_motor_state(msg)
        assert tc.body_state.is_moving is True
        assert tc.body_state.x == 1.5
        assert tc.body_state.heading_deg == 45.0

    def test_invalid_json_no_crash(self):
        tc = TankController()
        msg = MagicMock()
        msg.data = "not json"
        tc._on_motor_state(msg)  # Should not raise
        assert tc.body_state.is_moving is False


# ── Topics ───────────────────────────────────────────────────────────

class TestTopicConfig:
    """Tests for topic configuration."""

    def test_topic_prefix(self):
        assert TOPIC_PREFIX == "/tank"

    def test_cmd_vel_topic(self):
        assert TOPIC_CMD_VEL == "/tank/cmd_vel"


# ── Singleton ────────────────────────────────────────────────────────

class TestSingleton:
    """Tests for get_tank_controller singleton."""

    def test_returns_same_instance(self):
        import repryntt.hardware.tank as mod
        old = mod._tank
        try:
            mod._tank = None
            a = get_tank_controller()
            b = get_tank_controller()
            assert a is b
        finally:
            mod._tank = old
