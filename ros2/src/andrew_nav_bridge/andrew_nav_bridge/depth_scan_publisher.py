"""
depth_scan_publisher — converts Depth Anything V2 output → ROS2 LaserScan.

Andrew's depth pipeline (DepthEstimator) produces a full dense depth map
from the IMX219 camera using Depth Anything V2 running on the Jetson GPU.
This node converts that map to a /scan LaserScan that Nav2 + slam_toolbox
can use for costmap building and path planning.

Conversion:
    - Takes the bottom half of the depth map (floor-level obstacles)
    - Each image column → one laser ray at the corresponding horizontal angle
    - proximity (0=far, 1=close) → distance (metres) via calibration curve
    - IMX219 horizontal FOV: ~62 degrees
    - Published at ~5 Hz (depth model runs at ~32ms on Orin Nano)
"""

import sys
import math
import time
from pathlib import Path

_repo_root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_repo_root))

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import LaserScan
import numpy as np

# IMX219 horizontal field of view in radians (~62 degrees)
HFOV_RAD = math.radians(62.0)

# Calibration: proximity → distance in metres
# Derived from depth_perception.py: 0.8≈20cm, 0.5≈100cm, 0.2≈300cm
_PROX_PTS = np.array([0.0,  0.1,  0.2,  0.35, 0.5,  0.65, 0.8,  1.0])
_DIST_PTS = np.array([6.0,  4.5,  3.0,  1.8,  1.0,  0.5,  0.2,  0.05])

# Nav2 scan limits
RANGE_MIN = 0.05   # metres — ignore closer than this
RANGE_MAX = 5.0    # metres — ignore farther than this

PUBLISH_HZ = 5.0


def proximity_to_distance(prox_array: np.ndarray) -> np.ndarray:
    """Convert proximity (0–1) to distance (metres) via calibration curve."""
    return np.interp(prox_array, _PROX_PTS, _DIST_PTS).astype(np.float32)


class DepthScanPublisher(Node):

    def __init__(self):
        super().__init__("andrew_depth_scan_publisher")

        self._pub = self.create_publisher(LaserScan, "/scan", 10)
        self._timer = self.create_timer(1.0 / PUBLISH_HZ, self._publish_scan)

        # Lazy-load depth estimator on first tick
        self._depth = None
        self._camera = None
        self._frame_id = "base_scan"  # TF frame Nav2 expects

        self.get_logger().info(
            f"andrew_depth_scan_publisher ready | "
            f"HFOV={math.degrees(HFOV_RAD):.0f}° "
            f"range={RANGE_MIN}–{RANGE_MAX}m "
            f"@ {PUBLISH_HZ:.0f}Hz"
        )

    def _ensure_loaded(self) -> bool:
        if self._depth is not None:
            return True
        try:
            from repryntt.hardware.depth_perception import DepthEstimator
            from repryntt.hardware.camera_broker import get_camera_broker
            self._depth = DepthEstimator(use_half_res=True)
            self._camera = get_camera_broker()
            self.get_logger().info("Depth Anything V2 loaded — /scan publishing active")
            return True
        except Exception as e:
            self.get_logger().warn(f"Depth estimator not ready yet: {e}")
            return False

    def _publish_scan(self):
        if not self._ensure_loaded():
            return

        # Grab latest frame from camera broker (sensor 0)
        try:
            frame = self._camera.get_latest(0)
        except Exception as e:
            self.get_logger().debug(f"Camera frame unavailable: {e}")
            return

        if frame is None:
            return

        # Run depth estimation
        try:
            result = self._depth.estimate(frame)
        except Exception as e:
            self.get_logger().warn(f"Depth estimation failed: {e}")
            return

        # Get proximity map — shape (H, W), values 0=far 1=close
        prox_map = result._proximity  # numpy array

        # Use bottom half of image — floor-level obstacles matter for driving
        h, w = prox_map.shape
        bottom = prox_map[h // 2:, :]

        # Per-column: take the MAX proximity (nearest obstacle in that column)
        col_prox = np.max(bottom, axis=0)  # shape (W,)

        # Convert proximity → distance in metres
        distances = proximity_to_distance(col_prox)

        # Build LaserScan — columns map left→right, so angles go right→left
        # in ROS convention (positive = left/CCW from robot forward)
        num_rays = len(distances)
        angle_min = -HFOV_RAD / 2.0
        angle_max =  HFOV_RAD / 2.0
        angle_increment = HFOV_RAD / (num_rays - 1)

        # Flip so left side of image = positive angle (ROS: CCW = positive)
        distances_ros = distances[::-1].copy()

        # Clamp to valid range — Nav2 ignores inf/nan rays gracefully
        distances_ros = np.where(
            (distances_ros >= RANGE_MIN) & (distances_ros <= RANGE_MAX),
            distances_ros,
            float("inf"),
        )

        msg = LaserScan()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self._frame_id
        msg.angle_min = angle_min
        msg.angle_max = angle_max
        msg.angle_increment = angle_increment
        msg.time_increment = 0.0
        msg.scan_time = 1.0 / PUBLISH_HZ
        msg.range_min = RANGE_MIN
        msg.range_max = RANGE_MAX
        msg.ranges = distances_ros.tolist()

        self._pub.publish(msg)

        self.get_logger().debug(
            f"scan published: {num_rays} rays, "
            f"min_dist={float(np.min(distances_ros[np.isfinite(distances_ros)])):.2f}m"
        )


def main(args=None):
    rclpy.init(args=args)
    node = DepthScanPublisher()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
