"""
repryntt.hardware.ros2_publisher — singleton ROS2 publisher for repryntt.

Single point of ROS2 output for the repryntt process:
  - /scan          (sensor_msgs/LaserScan)   from Depth Anything V2
  - /cmd_vel_brain (geometry_msgs/Twist)     from nav_cortex motor decisions

Nav2 publishes to /cmd_vel_nav.
twist_mux merges both → /cmd_vel → cmd_vel_bridge → GPIO → motors.
Nav2 has higher priority so it overrides free-roam when a goal is active.

Lazy init: does nothing until first publish call. If ROS2 is not running
or rclpy is unavailable, all calls silently no-op.
"""

from __future__ import annotations

import math
import logging
import threading
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)

# IMX219 horizontal FOV
_HFOV_RAD = math.radians(62.0)

# Calibration: proximity (0=far,1=close) → distance metres
# depth_perception.py: 0.8≈20cm, 0.5≈100cm, 0.2≈300cm
_PROX_PTS = [0.0,  0.1,  0.2,  0.35, 0.5,  0.65, 0.8,  1.0]
_DIST_PTS = [6.0,  4.5,  3.0,  1.8,  1.0,  0.5,  0.2,  0.05]

# Nav action → (linear_x, angular_z) — magnitudes, speed applied as scalar
_ACTION_TWIST = {
    "forward":    ( 1.0,  0.0),
    "backward":   (-1.0,  0.0),
    "turn_left":  ( 0.0,  1.0),
    "turn_right": ( 0.0, -1.0),
    "stop":       ( 0.0,  0.0),
}

# Max physical velocities (metres/sec, rad/sec) — scale speed fraction against these
_MAX_LINEAR  = 0.30
_MAX_ANGULAR = 1.50


class _Ros2Publisher:
    """Internal singleton — use module-level helpers instead."""

    def __init__(self):
        self._lock = threading.Lock()
        self._node = None
        self._scan_pub = None
        self._cmd_pub = None
        self._executor = None
        self._thread = None
        self._init_attempted = False

    def _ensure_init(self) -> bool:
        with self._lock:
            if self._init_attempted:
                return self._node is not None
            self._init_attempted = True

        try:
            import rclpy
            from rclpy.node import Node
            from rclpy.executors import SingleThreadedExecutor
            from sensor_msgs.msg import LaserScan
            from geometry_msgs.msg import Twist

            if not rclpy.ok():
                rclpy.init()

            node = rclpy.create_node("repryntt_ros2_publisher")
            node.create_publisher(LaserScan, "/scan", 10)
            node.create_publisher(Twist, "/cmd_vel_brain", 10)

            executor = SingleThreadedExecutor()
            executor.add_node(node)

            t = threading.Thread(
                target=self._spin, args=(executor,), daemon=True, name="repryntt-ros2-spin"
            )
            t.start()

            with self._lock:
                self._node = node
                self._scan_pub = node.get_publisher(  # type: ignore[attr-defined]
                    "/scan")
                self._cmd_pub  = node.get_publisher(  # type: ignore[attr-defined]
                    "/cmd_vel_brain")
                self._executor = executor
                self._thread = t

            logger.info("repryntt ROS2 publisher initialised (/scan + /cmd_vel_brain)")
            return True

        except Exception as e:
            logger.debug(f"ROS2 publisher init skipped: {e}")
            return False

    def _spin(self, executor):
        try:
            while True:
                executor.spin_once(timeout_sec=0.05)
        except Exception:
            pass

    def _scan_pub_handle(self):
        if not self._ensure_init():
            return None
        with self._lock:
            return self._scan_pub

    def _cmd_pub_handle(self):
        if not self._ensure_init():
            return None
        with self._lock:
            return self._cmd_pub

    def publish_scan(self, prox_map) -> bool:
        """Publish a LaserScan from a DA2 proximity map (H×W numpy array).

        prox_map: numpy array, values 0=far 1=close (from DepthResult._proximity).
        """
        pub = self._scan_pub_handle()
        if pub is None:
            return False

        try:
            import numpy as np
            from sensor_msgs.msg import LaserScan

            # Bottom half only — floor-level obstacles
            h, w = prox_map.shape
            bottom = prox_map[h // 2:, :]
            col_prox = np.max(bottom, axis=0).astype(np.float32)

            # Proximity → metres via calibration curve
            distances = np.interp(col_prox, _PROX_PTS, _DIST_PTS).astype(np.float32)

            num_rays = len(distances)
            angle_min = -_HFOV_RAD / 2.0
            angle_max =  _HFOV_RAD / 2.0

            # Flip: left side of image → positive angle (ROS CCW convention)
            distances_ros = distances[::-1].copy()
            distances_ros = np.where(
                (distances_ros >= 0.05) & (distances_ros <= 5.0),
                distances_ros,
                float("inf"),
            )

            msg = LaserScan()
            msg.header.stamp = self._node.get_clock().now().to_msg()
            msg.header.frame_id = "base_scan"
            msg.angle_min = angle_min
            msg.angle_max = angle_max
            msg.angle_increment = _HFOV_RAD / max(num_rays - 1, 1)
            msg.time_increment = 0.0
            msg.scan_time = 0.2
            msg.range_min = 0.05
            msg.range_max = 5.0
            msg.ranges = distances_ros.tolist()
            pub.publish(msg)
            return True

        except Exception as e:
            logger.debug(f"scan publish failed: {e}")
            return False

    def publish_cmd_vel(self, action: str, speed: float = 0.6) -> bool:
        """Publish a Twist on /cmd_vel_brain for a nav_cortex action string."""
        pub = self._cmd_pub_handle()
        if pub is None:
            return False

        try:
            from geometry_msgs.msg import Twist

            lin_sign, ang_sign = _ACTION_TWIST.get(action, (0.0, 0.0))

            msg = Twist()
            msg.linear.x  = lin_sign  * speed * _MAX_LINEAR
            msg.angular.z = ang_sign  * speed * _MAX_ANGULAR
            pub.publish(msg)
            return True

        except Exception as e:
            logger.debug(f"cmd_vel publish failed: {e}")
            return False


# ── Singleton ────────────────────────────────────────────────────────

_publisher: Optional[_Ros2Publisher] = None
_pub_lock = threading.Lock()


def get_ros2_publisher() -> _Ros2Publisher:
    global _publisher
    with _pub_lock:
        if _publisher is None:
            _publisher = _Ros2Publisher()
    return _publisher


def publish_scan(prox_map) -> bool:
    return get_ros2_publisher().publish_scan(prox_map)


def publish_cmd_vel(action: str, speed: float = 0.6) -> bool:
    return get_ros2_publisher().publish_cmd_vel(action, speed)
