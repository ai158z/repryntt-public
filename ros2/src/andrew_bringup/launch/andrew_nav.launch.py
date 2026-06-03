"""
Launch file: andrew_nav.launch.py

Full autonomous navigation stack for Andrew.

Topic flow:
  repryntt brain  → /cmd_vel_brain  ──┐
                                       ├─ twist_mux → /cmd_vel → cmd_vel_bridge → GPIO
  Nav2 planner    → /cmd_vel_nav    ──┘  (Nav2 priority=100 overrides brain priority=10)

  repryntt DA2    → /scan           → slam_toolbox (map) + Nav2 (costmap)

Usage:
    source ~/.bashrc
    ros2 launch andrew_bringup andrew_nav.launch.py
"""

import os
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription, DeclareLaunchArgument
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    bringup_dir     = get_package_share_directory("andrew_bringup")
    description_dir = get_package_share_directory("andrew_description")
    nav2_bringup    = get_package_share_directory("nav2_bringup")

    params_file  = LaunchConfiguration("params_file")
    use_sim_time = LaunchConfiguration("use_sim_time")

    urdf_path = os.path.join(description_dir, "urdf", "andrew.urdf")
    with open(urdf_path) as f:
        robot_description = f.read()

    return LaunchDescription([
        DeclareLaunchArgument(
            "params_file",
            default_value=os.path.join(bringup_dir, "config", "nav2_params.yaml"),
            description="Full path to Nav2/slam_toolbox params YAML",
        ),
        DeclareLaunchArgument(
            "use_sim_time",
            default_value="false",
            description="Use simulation clock",
        ),

        # ── 1. Robot state publisher (TF from URDF) ──────────────────
        Node(
            package="robot_state_publisher",
            executable="robot_state_publisher",
            name="robot_state_publisher",
            output="screen",
            parameters=[{
                "robot_description": robot_description,
                "use_sim_time": use_sim_time,
            }],
        ),

        # ── 2. slam_toolbox — map building + localisation ─────────────
        Node(
            package="slam_toolbox",
            executable="async_slam_toolbox_node",
            name="slam_toolbox",
            output="screen",
            parameters=[params_file, {"use_sim_time": use_sim_time}],
        ),

        # ── 3. Nav2 — path planner + controller (publishes /cmd_vel_nav)
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(
                os.path.join(nav2_bringup, "launch", "navigation_launch.py")
            ),
            launch_arguments={
                "params_file": params_file,
                "use_sim_time": use_sim_time,
                "use_composition": "False",
            }.items(),
        ),

        # ── 4. twist_mux — Nav2 (priority 100) + brain (priority 10) → /cmd_vel
        Node(
            package="twist_mux",
            executable="twist_mux",
            name="twist_mux",
            output="screen",
            parameters=[os.path.join(bringup_dir, "config", "twist_mux.yaml")],
            remappings=[("cmd_vel_out", "/cmd_vel")],
        ),

        # ── 5. cmd_vel_bridge — /cmd_vel → tank GPIO (only motor driver)
        Node(
            package="andrew_nav_bridge",
            executable="cmd_vel_bridge",
            name="andrew_cmd_vel_bridge",
            output="screen",
        ),
    ])
