"""
cmd_vel_bridge — ROS2 node that translates /cmd_vel (Twist) → tank motors.

Differential drive kinematics:
    left_vel  = linear.x - angular.z * TRACK_SEPARATION / 2
    right_vel = linear.x + angular.z * TRACK_SEPARATION / 2

Then normalised to [-1, 1] against MAX_LINEAR_VEL.

A watchdog thread stops the motors if no cmd_vel arrives within
WATCHDOG_TIMEOUT seconds — safety guarantee if Nav2 dies mid-move.
"""

import sys
import threading
import time
from pathlib import Path

# Add a source checkout to the path for editable ROS2 workspaces.
_repo_root = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_repo_root))

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist

# Tank parameters — adjust these to match Andrew's physical chassis
TRACK_SEPARATION = 0.18   # metres, track centre-to-centre distance
MAX_LINEAR_VEL   = 0.30   # m/s at full motor speed (measure & calibrate)
MAX_ANGULAR_VEL  = 1.50   # rad/s at full differential (measure & calibrate)
WATCHDOG_TIMEOUT = 0.5    # seconds — stop if no cmd_vel received


class CmdVelBridgeNode(Node):

    def __init__(self):
        super().__init__("andrew_cmd_vel_bridge")

        self._last_cmd_time = time.time()
        self._lock = threading.Lock()
        self._motor_cm = None
        self._motor_session = None

        self._sub = self.create_subscription(
            Twist,
            "/cmd_vel",
            self._on_cmd_vel,
            10,
        )

        # Watchdog: stop motors if cmd_vel goes silent
        self._watchdog = self.create_timer(0.1, self._watchdog_tick)

        self.get_logger().info(
            f"andrew_cmd_vel_bridge ready | "
            f"track_sep={TRACK_SEPARATION}m  "
            f"max_linear={MAX_LINEAR_VEL}m/s  "
            f"watchdog={WATCHDOG_TIMEOUT}s"
        )

    def _on_cmd_vel(self, msg: Twist):
        lin = msg.linear.x
        ang = msg.angular.z

        # Differential drive kinematics
        left_mps  = lin - ang * TRACK_SEPARATION / 2.0
        right_mps = lin + ang * TRACK_SEPARATION / 2.0

        # Normalise to [-1, 1]
        left_norm  = left_mps  / MAX_LINEAR_VEL
        right_norm = right_mps / MAX_LINEAR_VEL

        # Clamp — don't exceed 1.0 even if Nav2 overshoots
        left_norm  = max(-1.0, min(1.0, left_norm))
        right_norm = max(-1.0, min(1.0, right_norm))

        self.get_logger().debug(
            f"cmd_vel lin={lin:.2f} ang={ang:.2f} "
            f"→ L={left_norm:+.2f} R={right_norm:+.2f}"
        )

        with self._lock:
            self._last_cmd_time = time.time()

        sess = self._ensure_motor_session()
        if sess is not None:
            try:
                sess.drive_continuous(left_norm, right_norm)
            except Exception as e:
                self.get_logger().warn(f"motor daemon command failed: {e}")
                self._close_motor_session()

    def _watchdog_tick(self):
        with self._lock:
            age = time.time() - self._last_cmd_time

        if age > WATCHDOG_TIMEOUT:
            if self._motor_session is not None:
                self.get_logger().warn(
                    f"cmd_vel silent for {age:.1f}s — stopping motors"
                )
                try:
                    self._motor_session.drive_continuous(0.0, 0.0)
                except Exception as e:
                    self.get_logger().debug(f"watchdog stop failed: {e}")
                self._close_motor_session()

    def _ensure_motor_session(self):
        if self._motor_session is not None and not self._motor_session.preempted:
            return self._motor_session
        self._close_motor_session()
        try:
            from repryntt.hardware.motor_client import Priority, session
            self._motor_cm = session(
                priority=Priority.AUTONOMOUS,
                holder_label="cmd_vel_bridge",
                wait_timeout_s=0.2,
                require_daemon=True,
            )
            self._motor_session = self._motor_cm.__enter__()
            self.get_logger().info("motor daemon lease acquired for cmd_vel")
            return self._motor_session
        except Exception as e:
            self.get_logger().warn(f"motor daemon unavailable for cmd_vel: {e}")
            self._motor_cm = None
            self._motor_session = None
            return None

    def _close_motor_session(self):
        if self._motor_cm is not None:
            try:
                self._motor_cm.__exit__(None, None, None)
            except Exception as e:
                self.get_logger().debug(f"motor session close failed: {e}")
        self._motor_cm = None
        self._motor_session = None

    def destroy_node(self):
        if self._motor_session is not None:
            try:
                self._motor_session.drive_continuous(0.0, 0.0)
            except Exception:
                pass
        self._close_motor_session()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = CmdVelBridgeNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
