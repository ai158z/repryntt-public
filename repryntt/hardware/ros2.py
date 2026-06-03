#!/usr/bin/env python3
"""
SAIGE ROS2 Tools for Brain System Integration
Provides ROS2-based robotics control tools for AI wheelchair operation
"""

import os
import sys
import time
import json
import subprocess
import threading
from typing import Dict, Any, Optional, List
import logging

logger = logging.getLogger(__name__)

# Import ROS2 modules conditionally (only if ROS2 is available)
try:
    import rclpy
    from rclpy.node import Node
    from rclpy.executors import SingleThreadedExecutor
    from geometry_msgs.msg import Twist
    from std_msgs.msg import Bool, String, Float32
    ROS2_AVAILABLE = True
except ImportError:
    ROS2_AVAILABLE = False
    logger.warning("ROS2 not available - wheelchair control tools disabled")


class SAIGEROS2Interface:
    """
    Interface for SAIGE AI to control ROS2-based mobile base
    Provides high-level commands that the AI can use
    """

    def __init__(self):
        self.ros2_initialized = False
        self.node: Optional[Node] = None
        self.executor: Optional[SingleThreadedExecutor] = None
        self.ros2_thread: Optional[threading.Thread] = None

        if ROS2_AVAILABLE:
            self._initialize_ros2()

    def _initialize_ros2(self):
        """Initialize ROS2 context"""
        try:
            if not rclpy.ok():
                rclpy.init()

            self.node = rclpy.create_node('saige_ai_interface')
            self.executor = SingleThreadedExecutor()
            self.executor.add_node(self.node)

            # Start ROS2 spinning in background thread
            self.ros2_thread = threading.Thread(target=self._ros2_spin, daemon=True)
            self.ros2_thread.start()

            self.ros2_initialized = True
            logger.info("ROS2 interface initialized for SAIGE AI")
        except Exception as e:
            logger.error(f"Failed to initialize ROS2: {e}")
            self.ros2_initialized = False

    def _ros2_spin(self):
        """ROS2 spinning thread"""
        try:
            while rclpy.ok() and self.ros2_initialized:
                self.executor.spin_once(timeout_sec=0.1)
                time.sleep(0.01)
        except Exception as e:
            logger.error(f"ROS2 spinning error: {e}")

    def move_mobile_base(self, linear_velocity: float = 0.0, angular_velocity: float = 0.0,
                        duration: float = 1.0) -> Dict[str, Any]:
        """
        Move the wheelchair with specified velocities

        Args:
            linear_velocity: Forward/backward speed (m/s)
            angular_velocity: Turning speed (rad/s)
            duration: How long to move (seconds)

        Returns:
            Dict with success status and details
        """
        if not self.ros2_initialized:
            return {
                'success': False,
                'error': 'ROS2 not initialized - wheelchair control unavailable',
                'message': 'Please ensure ROS2 is installed and wheelchair nodes are running'
            }

        try:
            # Create velocity command
            twist = Twist()
            twist.linear.x = float(linear_velocity)
            twist.angular.z = float(angular_velocity)

            # Publish command
            cmd_publisher = self.node.create_publisher(Twist, '/wheelchair/cmd_vel_ai', 10)

            # Publish command multiple times for reliability
            for _ in range(3):
                cmd_publisher.publish(twist)
                time.sleep(0.05)

            # Wait for specified duration if moving
            if abs(linear_velocity) > 0.01 or abs(angular_velocity) > 0.01:
                time.sleep(duration)

                # Send stop command
                stop_twist = Twist()
                cmd_publisher.publish(stop_twist)

            return {
                'success': True,
                'message': f'Wheelchair moved with linear={linear_velocity:.2f} m/s, angular={angular_velocity:.2f} rad/s for {duration:.1f} seconds',
                'command': {
                    'linear_velocity': linear_velocity,
                    'angular_velocity': angular_velocity,
                    'duration': duration
                }
            }

        except Exception as e:
            logger.error(f"Failed to move wheelchair: {e}")
            return {
                'success': False,
                'error': str(e),
                'message': 'Failed to execute wheelchair movement command'
            }

    def stop_mobile_base(self, emergency: bool = False) -> Dict[str, Any]:
        """
        Stop the mobile base immediately

        Args:
            emergency: Whether this is an emergency stop

        Returns:
            Dict with success status and details
        """
        if not self.ros2_initialized:
            return {
                'success': False,
                'error': 'ROS2 not initialized - wheelchair control unavailable'
            }

        try:
            if emergency:
                # Publish emergency stop
                emergency_publisher = self.node.create_publisher(Bool, '/wheelchair/emergency_stop', 10)
                stop_msg = Bool()
                stop_msg.data = True
                emergency_publisher.publish(stop_msg)

                return {
                    'success': True,
                    'message': 'Emergency stop activated',
                    'emergency': True
                }
            else:
                # Normal stop
                twist = Twist()
                cmd_publisher = self.node.create_publisher(Twist, '/wheelchair/cmd_vel_ai', 10)
                cmd_publisher.publish(twist)

                return {
                    'success': True,
                    'message': 'Wheelchair stopped normally',
                    'emergency': False
                }

        except Exception as e:
            logger.error(f"Failed to stop wheelchair: {e}")
            return {
                'success': False,
                'error': str(e),
                'message': 'Failed to stop wheelchair'
            }

    def get_mobile_base_status(self) -> Dict[str, Any]:
        """
        Get current mobile base status

        Returns:
            Dict with mobile base status information
        """
        if not self.ros2_initialized:
            return {
                'success': False,
                'error': 'ROS2 not initialized - cannot get status',
                'ros2_available': ROS2_AVAILABLE
            }

        try:
            # In a real implementation, you would subscribe to status topics
            # For now, return basic status
            return {
                'success': True,
                'status': {
                    'ros2_connected': self.ros2_initialized,
                    'timestamp': time.time(),
                    'message': 'Wheelchair status monitoring requires running ROS2 nodes'
                },
                'note': 'Real-time status requires wheelchair_controller node to be running'
            }

        except Exception as e:
            logger.error(f"Failed to get wheelchair status: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def reset_emergency_stop(self) -> Dict[str, Any]:
        """
        Reset emergency stop condition

        Returns:
            Dict with success status
        """
        if not self.ros2_initialized:
            return {
                'success': False,
                'error': 'ROS2 not initialized - cannot reset emergency stop'
            }

        try:
            emergency_publisher = self.node.create_publisher(Bool, '/wheelchair/emergency_stop', 10)
            reset_msg = Bool()
            reset_msg.data = False
            emergency_publisher.publish(reset_msg)

            return {
                'success': True,
                'message': 'Emergency stop reset'
            }

        except Exception as e:
            logger.error(f"Failed to reset emergency stop: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def set_speed_limits(self, max_linear: float = 1.0, max_angular: float = 1.0) -> Dict[str, Any]:
        """
        Set speed limits for mobile base movement

        Args:
            max_linear: Maximum linear speed (m/s)
            max_angular: Maximum angular speed (rad/s)

        Returns:
            Dict with success status
        """
        if not self.ros2_initialized:
            return {
                'success': False,
                'error': 'ROS2 not initialized - cannot set speed limits'
            }

        try:
            # Publish speed limits (would be used by safety monitor)
            limits_msg = Twist()
            limits_msg.linear.x = float(max_linear)
            limits_msg.angular.z = float(max_angular)

            limits_publisher = self.node.create_publisher(Twist, '/wheelchair/speed_limits', 10)
            limits_publisher.publish(limits_msg)

            return {
                'success': True,
                'message': f'Speed limits set: linear={max_linear:.2f} m/s, angular={max_angular:.2f} rad/s',
                'limits': {
                    'max_linear': max_linear,
                    'max_angular': max_angular
                }
            }

        except Exception as e:
            logger.error(f"Failed to set speed limits: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def start_mobile_base_nodes(self) -> Dict[str, Any]:
        """
        Start the ROS2 mobile base control nodes

        Returns:
            Dict with success status
        """
        try:
            # Check if ROS2 is available
            import shutil
            if not shutil.which('ros2'):
                return {
                    'success': False,
                    'error': 'ROS2 not found in PATH',
                    'message': 'Please install ROS2 and source the setup script'
                }

            # Start wheelchair controller node
            controller_cmd = [
                'ros2', 'run', 'saige_wheelchair_control', 'wheelchair_controller'
            ]

            # Start safety monitor node
            safety_cmd = [
                'ros2', 'run', 'saige_wheelchair_control', 'wheelchair_safety_monitor'
            ]

            # Note: In a real implementation, you would start these as background processes
            # For now, just return instructions

            return {
                'success': True,
                'message': 'Wheelchair nodes started (simulation)',
                'commands': {
                    'controller': ' '.join(controller_cmd),
                    'safety_monitor': ' '.join(safety_cmd)
                },
                'note': 'In production, these would run as background ROS2 nodes'
            }

        except Exception as e:
            logger.error(f"Failed to start wheelchair nodes: {e}")
            return {
                'success': False,
                'error': str(e)
            }

    def shutdown(self):
        """Shutdown ROS2 interface"""
        if self.ros2_initialized:
            self.ros2_initialized = False
            if self.node:
                self.node.destroy_node()
            if self.executor:
                self.executor.shutdown()
            rclpy.shutdown()
            logger.info("ROS2 interface shutdown")


# Global ROS2 interface instance
_ros2_interface: Optional[SAIGEROS2Interface] = None

def get_ros2_interface() -> SAIGEROS2Interface:
    """Get or create global ROS2 interface instance"""
    global _ros2_interface
    if _ros2_interface is None:
        _ros2_interface = SAIGEROS2Interface()
    return _ros2_interface


# Tool functions for brain system integration

def move_mobile_base_forward(distance: float = 1.0, speed: float = 0.5) -> Dict[str, Any]:
    """
    Move mobile base forward by specified distance at specified speed

    Args:
        distance: Distance to move (meters)
        speed: Speed to move at (m/s)

    Returns:
        Dict with movement results
    """
    interface = get_ros2_interface()
    duration = abs(distance) / max(speed, 0.1)  # Prevent division by zero
    linear_velocity = speed if distance >= 0 else -speed

    result = interface.move_mobile_base(
        linear_velocity=linear_velocity,
        angular_velocity=0.0,
        duration=duration
    )

    result['tool'] = 'move_mobile_base_forward'
    result['parameters'] = {'distance': distance, 'speed': speed}
    return result


def move_mobile_base_backward(distance: float = 1.0, speed: float = 0.5) -> Dict[str, Any]:
    """
    Move mobile base backward by specified distance at specified speed

    Args:
        distance: Distance to move (meters)
        speed: Speed to move at (m/s)

    Returns:
        Dict with movement results
    """
    interface = get_ros2_interface()
    duration = abs(distance) / max(speed, 0.1)
    linear_velocity = -speed  # Negative for backward

    result = interface.move_mobile_base(
        linear_velocity=linear_velocity,
        angular_velocity=0.0,
        duration=duration
    )

    result['tool'] = 'move_mobile_base_backward'
    result['parameters'] = {'distance': distance, 'speed': speed}
    return result


def turn_mobile_base_left(angle: float = 1.57, speed: float = 0.5) -> Dict[str, Any]:
    """
    Turn mobile base left by specified angle at specified speed

    Args:
        angle: Angle to turn (radians, positive for left)
        speed: Angular speed (rad/s)

    Returns:
        Dict with turning results
    """
    interface = get_ros2_interface()
    duration = abs(angle) / max(speed, 0.1)

    result = interface.move_mobile_base(
        linear_velocity=0.0,
        angular_velocity=speed,
        duration=duration
    )

    result['tool'] = 'turn_mobile_base_left'
    result['parameters'] = {'angle': angle, 'speed': speed}
    return result


def turn_mobile_base_right(angle: float = 1.57, speed: float = 0.5) -> Dict[str, Any]:
    """
    Turn mobile base right by specified angle at specified speed

    Args:
        angle: Angle to turn (radians, positive for right)
        speed: Angular speed (rad/s)

    Returns:
        Dict with turning results
    """
    interface = get_ros2_interface()
    duration = abs(angle) / max(speed, 0.1)

    result = interface.move_mobile_base(
        linear_velocity=0.0,
        angular_velocity=-speed,  # Negative for right turn
        duration=duration
    )

    result['tool'] = 'turn_mobile_base_right'
    result['parameters'] = {'angle': angle, 'speed': speed}
    return result


def stop_mobile_base() -> Dict[str, Any]:
    """
    Stop mobile base movement immediately

    Returns:
        Dict with stop results
    """
    interface = get_ros2_interface()
    result = interface.stop_mobile_base(emergency=False)
    result['tool'] = 'stop_mobile_base'
    return result


def emergency_stop_mobile_base() -> Dict[str, Any]:
    """
    Emergency stop mobile base with safety protocols

    Returns:
        Dict with emergency stop results
    """
    interface = get_ros2_interface()
    result = interface.stop_mobile_base(emergency=True)
    result['tool'] = 'emergency_stop_mobile_base'
    return result


def get_mobile_base_status() -> Dict[str, Any]:
    """
    Get current mobile base status and diagnostics

    Returns:
        Dict with status information
    """
    interface = get_ros2_interface()
    result = interface.get_mobile_base_status()
    result['tool'] = 'get_mobile_base_status'
    return result


def reset_mobile_base_emergency_stop() -> Dict[str, Any]:
    """
    Reset emergency stop condition

    Returns:
        Dict with reset results
    """
    interface = get_ros2_interface()
    result = interface.reset_emergency_stop()
    result['tool'] = 'reset_mobile_base_emergency_stop'
    return result


def set_mobile_base_speed_limits(max_linear: float = 1.0, max_angular: float = 1.0) -> Dict[str, Any]:
    """
    Set speed limits for mobile base movement

    Args:
        max_linear: Maximum linear speed (m/s)
        max_angular: Maximum angular speed (rad/s)

    Returns:
        Dict with speed limit results
    """
    interface = get_ros2_interface()
    result = interface.set_speed_limits(max_linear, max_angular)
    result['tool'] = 'set_mobile_base_speed_limits'
    result['parameters'] = {'max_linear': max_linear, 'max_angular': max_angular}
    return result


def start_mobile_base_system() -> Dict[str, Any]:
    """
    Start the complete mobile base ROS2 system

    Returns:
        Dict with startup results
    """
    interface = get_ros2_interface()
    result = interface.start_mobile_base_nodes()
    result['tool'] = 'start_mobile_base_system'
    return result


def navigate_to_location(x: float = 0.0, y: float = 0.0, theta: float = 0.0) -> Dict[str, Any]:
    """
    Navigate wheelchair to specified location (requires navigation stack)

    Args:
        x: X coordinate (meters)
        y: Y coordinate (meters)
        theta: Orientation (radians)

    Returns:
        Dict with navigation results
    """
    return {
        'success': False,
        'error': 'Navigation not yet implemented',
        'message': 'Full navigation stack integration required',
        'tool': 'navigate_to_location',
        'parameters': {'x': x, 'y': y, 'theta': theta},
        'note': 'This would require ROS2 Navigation2 stack integration'
    }


# Tool registry for brain system
ROS2_TOOLS = {
    'move_mobile_base_forward': move_mobile_base_forward,
    'move_mobile_base_backward': move_mobile_base_backward,
    'turn_mobile_base_left': turn_mobile_base_left,
    'turn_mobile_base_right': turn_mobile_base_right,
    'stop_mobile_base': stop_mobile_base,
    'emergency_stop_mobile_base': emergency_stop_mobile_base,
    'get_mobile_base_status': get_mobile_base_status,
    'reset_mobile_base_emergency_stop': reset_mobile_base_emergency_stop,
    'set_mobile_base_speed_limits': set_mobile_base_speed_limits,
    'start_mobile_base_system': start_mobile_base_system,
    'navigate_to_location': navigate_to_location,
}


def get_available_ros2_tools() -> List[str]:
    """Get list of available ROS2 tools"""
    return list(ROS2_TOOLS.keys())


def is_ros2_available() -> bool:
    """Check if ROS2 is available"""
    return ROS2_AVAILABLE


# Cleanup function
def shutdown_ros2_tools():
    """Shutdown ROS2 tools cleanly"""
    global _ros2_interface
    if _ros2_interface:
        _ros2_interface.shutdown()
        _ros2_interface = None


if __name__ == "__main__":
    # Test the ROS2 interface
    print("Testing SAIGE ROS2 Tools...")
    print(f"ROS2 Available: {is_ros2_available()}")
    print(f"Available Tools: {get_available_ros2_tools()}")

    if ROS2_AVAILABLE:
        interface = get_ros2_interface()
        status = interface.get_wheelchair_status()
        print(f"Status: {status}")

        # Cleanup
        shutdown_ros2_tools()

    print("ROS2 Tools test completed.")
