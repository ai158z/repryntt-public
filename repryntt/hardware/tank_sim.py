"""
repryntt.hardware.tank_sim — 2D Tank Simulation for RL Training.

A lightweight Gymnasium environment that simulates the physical tank in a 2D
grid world. The agent learns navigation, obstacle avoidance, and goal-seeking
before transferring policies to the real hardware.

Matches the real tank's control interface:
    - Actions: forward, backward, turn_left, turn_right, stop (discrete)
    - Observations: simulated camera rays + position + heading
    - Reward: distance to goal, collision penalty, exploration bonus

No GPU required — runs on CPU. Designed for the Jetson Orin Nano's 8GB RAM.
"""

from __future__ import annotations

import math
import random
from typing import Any, Dict, Optional, Tuple

import numpy as np

try:
    import gymnasium as gym
    from gymnasium import spaces
    GYM_AVAILABLE = True
except ImportError:
    GYM_AVAILABLE = False

# ── Constants ────────────────────────────────────────────────────────

GRID_SIZE = 20          # 20x20 world (each cell ~30cm = 6m x 6m room)
CELL_SIZE_CM = 30       # cm per grid cell
TANK_SPEED_CELLS = 1.0  # cells per step when moving (≈2ft/s real)
TURN_DEGREES = 45       # degrees per turn action
NUM_RAYS = 8            # simulated distance sensor rays
MAX_RAY_DIST = 10       # max ray distance in cells
NUM_OBSTACLES = 8       # random obstacles in the world

# Actions
ACTION_FORWARD = 0
ACTION_BACKWARD = 1
ACTION_TURN_LEFT = 2
ACTION_TURN_RIGHT = 3
ACTION_STOP = 4


class TankSimEnv(gym.Env if GYM_AVAILABLE else object):
    """2D tank navigation environment.

    Observation space (continuous):
        [0:8]   - Ray distances (0-1, normalized) — simulated distance sensors
        [8]     - Heading (0-1, normalized from 0-360°)
        [9]     - Distance to goal (0-1, normalized)
        [10]    - Angle to goal (-1 to 1, normalized from -180° to 180°)

    Action space (discrete):
        0 = forward, 1 = backward, 2 = turn_left, 3 = turn_right, 4 = stop

    Rewards:
        +10.0  — Reaching the goal
        +0.1   — Getting closer to goal (per step)
        -0.1   — Getting further from goal
        -5.0   — Hitting an obstacle/wall
        -0.01  — Each step (encourages efficiency)
    """

    metadata = {"render_modes": ["ansi", "rgb_array"], "render_fps": 10}

    def __init__(self, render_mode: Optional[str] = None,
                 grid_size: int = GRID_SIZE,
                 num_obstacles: int = NUM_OBSTACLES):
        super().__init__()
        self.grid_size = grid_size
        self.num_obstacles = num_obstacles
        self.render_mode = render_mode

        # Spaces
        self.action_space = spaces.Discrete(5)
        # 8 rays + heading + dist_to_goal + angle_to_goal = 11
        self.observation_space = spaces.Box(
            low=-1.0, high=1.0, shape=(11,), dtype=np.float32
        )

        # State
        self.tank_x: float = 0.0
        self.tank_y: float = 0.0
        self.tank_heading: float = 0.0  # degrees, 0 = North/up
        self.goal_x: float = 0.0
        self.goal_y: float = 0.0
        self.obstacles: list = []
        self.steps: int = 0
        self.max_steps: int = 200
        self.prev_dist_to_goal: float = 0.0
        self.visited_cells: set = set()

    def reset(self, seed: Optional[int] = None, options: Optional[dict] = None):
        super().reset(seed=seed) if GYM_AVAILABLE else None
        if seed is not None:
            random.seed(seed)
            np.random.seed(seed)

        # Place tank at random position
        self.tank_x = random.uniform(1, self.grid_size - 2)
        self.tank_y = random.uniform(1, self.grid_size - 2)
        self.tank_heading = random.uniform(0, 360)

        # Place goal far enough from tank
        for _ in range(100):
            self.goal_x = random.uniform(1, self.grid_size - 2)
            self.goal_y = random.uniform(1, self.grid_size - 2)
            if self._dist_to_goal() > self.grid_size * 0.3:
                break

        # Generate obstacles (not on tank or goal)
        self.obstacles = []
        for _ in range(self.num_obstacles):
            for _attempt in range(50):
                ox = random.randint(1, self.grid_size - 2)
                oy = random.randint(1, self.grid_size - 2)
                dist_tank = math.sqrt((ox - self.tank_x)**2 + (oy - self.tank_y)**2)
                dist_goal = math.sqrt((ox - self.goal_x)**2 + (oy - self.goal_y)**2)
                if dist_tank > 2.0 and dist_goal > 2.0:
                    self.obstacles.append((ox, oy))
                    break

        self.steps = 0
        self.prev_dist_to_goal = self._dist_to_goal()
        self.visited_cells = {(int(self.tank_x), int(self.tank_y))}

        obs = self._get_obs()
        info = self._get_info()
        return obs, info

    def step(self, action: int):
        self.steps += 1
        reward = -0.01  # step penalty

        old_x, old_y = self.tank_x, self.tank_y

        if action == ACTION_FORWARD:
            rad = math.radians(self.tank_heading)
            self.tank_x += math.sin(rad) * TANK_SPEED_CELLS
            self.tank_y -= math.cos(rad) * TANK_SPEED_CELLS
        elif action == ACTION_BACKWARD:
            rad = math.radians(self.tank_heading)
            self.tank_x -= math.sin(rad) * TANK_SPEED_CELLS
            self.tank_y += math.cos(rad) * TANK_SPEED_CELLS
        elif action == ACTION_TURN_LEFT:
            self.tank_heading = (self.tank_heading - TURN_DEGREES) % 360
        elif action == ACTION_TURN_RIGHT:
            self.tank_heading = (self.tank_heading + TURN_DEGREES) % 360
        elif action == ACTION_STOP:
            pass  # no movement

        # Check wall collision
        hit_wall = False
        if (self.tank_x < 0.5 or self.tank_x > self.grid_size - 0.5 or
                self.tank_y < 0.5 or self.tank_y > self.grid_size - 0.5):
            hit_wall = True
            self.tank_x = max(0.5, min(self.grid_size - 0.5, self.tank_x))
            self.tank_y = max(0.5, min(self.grid_size - 0.5, self.tank_y))
            reward -= 5.0

        # Check obstacle collision
        hit_obstacle = False
        for ox, oy in self.obstacles:
            if math.sqrt((self.tank_x - ox)**2 + (self.tank_y - oy)**2) < 0.8:
                hit_obstacle = True
                self.tank_x, self.tank_y = old_x, old_y  # bounce back
                reward -= 5.0
                break

        # Goal check
        dist = self._dist_to_goal()
        reached_goal = dist < 1.0
        if reached_goal:
            reward += 10.0

        # Distance-based shaping
        if not hit_wall and not hit_obstacle:
            delta = self.prev_dist_to_goal - dist
            reward += delta * 0.5  # reward for getting closer

        # Exploration bonus
        cell = (int(self.tank_x), int(self.tank_y))
        if cell not in self.visited_cells:
            self.visited_cells.add(cell)
            reward += 0.02

        self.prev_dist_to_goal = dist

        terminated = reached_goal
        truncated = self.steps >= self.max_steps

        obs = self._get_obs()
        info = self._get_info()

        return obs, reward, terminated, truncated, info

    # ── Observation helpers ──────────────────────────────────────────

    def _get_obs(self) -> np.ndarray:
        """Build observation vector: 8 rays + heading + goal_dist + goal_angle."""
        rays = self._cast_rays()
        heading_norm = self.tank_heading / 360.0
        dist_norm = min(self._dist_to_goal() / (self.grid_size * 1.414), 1.0)
        angle_to_goal = self._angle_to_goal() / 180.0  # -1 to 1

        obs = np.array(
            rays + [heading_norm, dist_norm, angle_to_goal],
            dtype=np.float32
        )
        return obs

    def _cast_rays(self) -> list:
        """Cast NUM_RAYS distance rays from tank position. Returns normalized distances."""
        rays = []
        for i in range(NUM_RAYS):
            angle = self.tank_heading + (i * 360 / NUM_RAYS)
            rad = math.radians(angle)
            dx = math.sin(rad) * 0.5
            dy = -math.cos(rad) * 0.5

            dist = 0.0
            cx, cy = self.tank_x, self.tank_y
            for step in range(MAX_RAY_DIST * 2):
                cx += dx
                cy += dy
                dist += 0.5

                # Wall hit
                if cx < 0 or cx > self.grid_size or cy < 0 or cy > self.grid_size:
                    break

                # Obstacle hit
                hit = False
                for ox, oy in self.obstacles:
                    if math.sqrt((cx - ox)**2 + (cy - oy)**2) < 0.6:
                        hit = True
                        break
                if hit:
                    break

            rays.append(dist / MAX_RAY_DIST)  # normalize to 0-1
        return rays

    def _dist_to_goal(self) -> float:
        return math.sqrt((self.tank_x - self.goal_x)**2 + (self.tank_y - self.goal_y)**2)

    def _angle_to_goal(self) -> float:
        """Signed angle from current heading to goal, in degrees (-180 to 180)."""
        dx = self.goal_x - self.tank_x
        dy = self.goal_y - self.tank_y
        goal_angle = math.degrees(math.atan2(dx, -dy)) % 360
        diff = goal_angle - self.tank_heading
        if diff > 180:
            diff -= 360
        elif diff < -180:
            diff += 360
        return diff

    def _get_info(self) -> dict:
        return {
            "tank_pos": (round(self.tank_x, 2), round(self.tank_y, 2)),
            "tank_heading": round(self.tank_heading, 1),
            "goal_pos": (round(self.goal_x, 2), round(self.goal_y, 2)),
            "dist_to_goal": round(self._dist_to_goal(), 2),
            "steps": self.steps,
            "visited_cells": len(self.visited_cells),
        }

    # ── Rendering ────────────────────────────────────────────────────

    def render(self) -> Optional[str]:
        if self.render_mode == "ansi":
            return self._render_ansi()
        return None

    def _render_ansi(self) -> str:
        """ASCII art rendering of the world."""
        grid = [["." for _ in range(self.grid_size)] for _ in range(self.grid_size)]

        # Obstacles
        for ox, oy in self.obstacles:
            if 0 <= ox < self.grid_size and 0 <= oy < self.grid_size:
                grid[int(oy)][int(ox)] = "#"

        # Goal
        gx, gy = int(self.goal_x), int(self.goal_y)
        if 0 <= gx < self.grid_size and 0 <= gy < self.grid_size:
            grid[gy][gx] = "G"

        # Tank (with direction indicator)
        tx, ty = int(self.tank_x), int(self.tank_y)
        if 0 <= tx < self.grid_size and 0 <= ty < self.grid_size:
            h = self.tank_heading % 360
            if h < 22.5 or h >= 337.5:
                arrow = "^"
            elif h < 67.5:
                arrow = "/"
            elif h < 112.5:
                arrow = ">"
            elif h < 157.5:
                arrow = "\\"
            elif h < 202.5:
                arrow = "v"
            elif h < 247.5:
                arrow = "/"
            elif h < 292.5:
                arrow = "<"
            else:
                arrow = "\\"
            grid[ty][tx] = arrow

        lines = ["+" + "-" * self.grid_size + "+"]
        for row in grid:
            lines.append("|" + "".join(row) + "|")
        lines.append("+" + "-" * self.grid_size + "+")
        lines.append(f"Pos: ({self.tank_x:.1f}, {self.tank_y:.1f}) "
                      f"Heading: {self.tank_heading:.0f}° "
                      f"Goal dist: {self._dist_to_goal():.1f} "
                      f"Steps: {self.steps}")
        return "\n".join(lines)


# ── Simple Q-learning agent for training ─────────────────────────────

class TankQLearner:
    """Tabular Q-learning agent that discretizes observations.

    Lightweight enough to train on Jetson CPU. Andrew can run episodes
    and learn a basic navigation policy, then transfer the behavioral
    patterns to real-world movement.
    """

    def __init__(self, n_actions: int = 5, learning_rate: float = 0.1,
                 discount: float = 0.95, epsilon: float = 1.0,
                 epsilon_decay: float = 0.995, epsilon_min: float = 0.05):
        self.n_actions = n_actions
        self.lr = learning_rate
        self.gamma = discount
        self.epsilon = epsilon
        self.epsilon_decay = epsilon_decay
        self.epsilon_min = epsilon_min
        self.q_table: Dict[tuple, np.ndarray] = {}
        self.episode_rewards: list = []

    def _discretize(self, obs: np.ndarray) -> tuple:
        """Convert continuous observation to discrete state key."""
        # Bin each observation dimension into 5 buckets
        bins = np.digitize(obs, bins=np.linspace(-1, 1, 6)[1:-1])
        return tuple(bins)

    def choose_action(self, obs: np.ndarray) -> int:
        if random.random() < self.epsilon:
            return random.randint(0, self.n_actions - 1)
        state = self._discretize(obs)
        q_vals = self.q_table.get(state, np.zeros(self.n_actions))
        return int(np.argmax(q_vals))

    def learn(self, obs: np.ndarray, action: int, reward: float,
              next_obs: np.ndarray, done: bool) -> None:
        state = self._discretize(obs)
        next_state = self._discretize(next_obs)

        if state not in self.q_table:
            self.q_table[state] = np.zeros(self.n_actions)
        if next_state not in self.q_table:
            self.q_table[next_state] = np.zeros(self.n_actions)

        current_q = self.q_table[state][action]
        if done:
            target = reward
        else:
            target = reward + self.gamma * np.max(self.q_table[next_state])
        self.q_table[state][action] += self.lr * (target - current_q)

    def decay_epsilon(self) -> None:
        self.epsilon = max(self.epsilon_min, self.epsilon * self.epsilon_decay)

    def train(self, env: TankSimEnv, episodes: int = 500,
              verbose: bool = True) -> Dict[str, Any]:
        """Train the agent on the environment."""
        total_rewards = []
        goals_reached = 0

        for ep in range(episodes):
            obs, info = env.reset()
            ep_reward = 0.0
            done = False

            while not done:
                action = self.choose_action(obs)
                next_obs, reward, terminated, truncated, info = env.step(action)
                done = terminated or truncated

                self.learn(obs, action, reward, next_obs, done)
                obs = next_obs
                ep_reward += reward

            if terminated:  # reached goal
                goals_reached += 1

            total_rewards.append(ep_reward)
            self.decay_epsilon()

            if verbose and (ep + 1) % 50 == 0:
                avg = np.mean(total_rewards[-50:])
                print(f"  Episode {ep+1}/{episodes}: avg_reward={avg:.2f}, "
                      f"epsilon={self.epsilon:.3f}, goals={goals_reached}, "
                      f"states={len(self.q_table)}")

        self.episode_rewards = total_rewards
        return {
            "episodes": episodes,
            "goals_reached": goals_reached,
            "goal_rate": round(goals_reached / episodes, 3),
            "final_avg_reward": round(float(np.mean(total_rewards[-50:])), 2),
            "q_table_size": len(self.q_table),
            "final_epsilon": round(self.epsilon, 4),
        }

    def get_policy_summary(self) -> str:
        """Human-readable summary of what the agent learned."""
        action_names = ["forward", "backward", "turn_left", "turn_right", "stop"]
        action_counts = np.zeros(5)

        for state, q_vals in self.q_table.items():
            best = int(np.argmax(q_vals))
            action_counts[best] += 1

        total = sum(action_counts)
        if total == 0:
            return "No policy learned yet."

        lines = ["Learned policy distribution:"]
        for i, name in enumerate(action_names):
            pct = action_counts[i] / total * 100
            lines.append(f"  {name}: {pct:.1f}% of states")
        return "\n".join(lines)
