"""
repryntt.hardware.local_perception — Local Obstacle Detection + Occupancy Grid.

This is the missing "fast eyes" — runs entirely on-device, no API calls.
Replaces the 3-5 second Gemini API call with ~100-200ms local stereo depth.

Two components:
    1. LocalObstacleDetector — fast stereo depth → obstacle zones
       Uses the existing StereoSGBM matcher but with OpenCV VideoCapture
       instead of shelling out to gst-launch each frame. 10-20x faster.
       
    2. OccupancyGrid — 2D numpy array (10cm resolution) tracking
       free/occupied/unknown cells. Updated from stereo depth + dead reckoning.
       Enables real path planning instead of random wandering.

This is what iRobot, Tesla, and BD all have at Layer 1.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

logger = logging.getLogger(__name__)

# ── Stereo camera calibration (Waveshare IMX219-83) ──────────────────

BASELINE_CM = 8.0        # distance between cameras
FOCAL_PX = 723.0         # focal length in pixels at 1280 wide
CLOSE_DISP_THRESH = 20   # disparity > this = "close" object
DANGER_DIST_CM = 20      # closer than this = emergency stop


@dataclass
class ObstacleReading:
    """Fast local obstacle reading from stereo depth."""
    left: float            # 0=clear, 1=blocked
    center: float
    right: float
    min_distance_cm: float
    capture_ms: float
    compute_ms: float
    valid: bool = True


class LocalObstacleDetector:
    """Fast stereo obstacle detection — no API calls, runs locally.

    Pulls synchronized frames from the camera_broker (single-producer per
    CSI sensor) and runs StereoSGBM. The broker owns the camera handles;
    this class never opens /dev/video* itself.

    Target: <200ms per reading (vs 3-5s with Gemini API).
    """

    def __init__(self):
        self._matcher = None
        self._last_reading: Optional[ObstacleReading] = None

        if CV2_AVAILABLE:
            self._matcher = cv2.StereoSGBM_create(
                minDisparity=0,
                numDisparities=64,
                blockSize=9,
                P1=8 * 3 * 9**2,
                P2=32 * 3 * 9**2,
                disp12MaxDiff=1,
                uniquenessRatio=10,
                speckleWindowSize=100,
                speckleRange=32,
            )

    def read(self) -> Optional[ObstacleReading]:
        """Take a single stereo reading — returns obstacle zones.

        Pulls synchronized frames from the broker. ~100-200ms typical.
        """
        if not CV2_AVAILABLE or self._matcher is None:
            return None

        t0 = time.time()
        try:
            from repryntt.hardware.camera_broker import broker
            frame_l, frame_r, _ts = broker.get_latest_pair(
                sensor_ids=(0, 1), sync_tolerance_ms=80.0,
                max_age_ms=500, timeout_s=2.0,
            )
        except Exception as e:
            logger.warning(f"camera_broker stereo fetch failed: {e}")
            return self._read_fallback()
        capture_ms = (time.time() - t0) * 1000

        if frame_l is None or frame_r is None:
            logger.warning("Stereo frame grab failed (broker returned None)")
            return self._read_fallback()

        t1 = time.time()

        # Convert to grayscale for stereo matching
        gray_l = cv2.cvtColor(frame_l, cv2.COLOR_BGR2GRAY)
        gray_r = cv2.cvtColor(frame_r, cv2.COLOR_BGR2GRAY)

        # Compute disparity
        disparity = self._matcher.compute(gray_l, gray_r).astype(np.float32) / 16.0

        # Analyze zones (left third, center third, right third)
        h, w = disparity.shape
        third = w // 3
        zones = {
            "left": disparity[:, :third],
            "center": disparity[:, third:2*third],
            "right": disparity[:, 2*third:],
        }

        proximities = {}
        min_distance = 500.0

        for name, zone in zones.items():
            valid = zone[zone > 0]
            if len(valid) > 0:
                close_frac = float(np.sum(valid > CLOSE_DISP_THRESH) / len(valid))
                proximities[name] = round(min(1.0, close_frac), 3)
                max_disp = float(np.percentile(valid, 95))
                if max_disp > 1:
                    dist = (BASELINE_CM * FOCAL_PX) / max_disp
                    # Scale focal length for 640 wide (calibrated at 1280)
                    dist *= 2.0  # focal_px was for 1280, we're at 640
                    min_distance = min(min_distance, dist)
            else:
                proximities[name] = 0.0

        compute_ms = (time.time() - t1) * 1000

        reading = ObstacleReading(
            left=proximities.get("left", 0.0),
            center=proximities.get("center", 0.0),
            right=proximities.get("right", 0.0),
            min_distance_cm=round(min_distance, 1),
            capture_ms=round(capture_ms, 1),
            compute_ms=round(compute_ms, 1),
        )
        self._last_reading = reading
        return reading

    def _read_fallback(self) -> Optional[ObstacleReading]:
        """Fallback using nav_cortex stereo capture (slower, shells out)."""
        try:
            from repryntt.hardware.nav_cortex import get_nav_cortex
            cortex = get_nav_cortex()
            depth = cortex.capture_stereo()
            if depth and depth.valid:
                return ObstacleReading(
                    left=depth.left_proximity,
                    center=depth.center_proximity,
                    right=depth.right_proximity,
                    min_distance_cm=depth.min_distance_cm,
                    capture_ms=depth.compute_time_ms * 0.7,
                    compute_ms=depth.compute_time_ms * 0.3,
                )
        except Exception as e:
            logger.debug(f"Fallback stereo failed: {e}")
        return None

    @property
    def last_reading(self) -> Optional[ObstacleReading]:
        return self._last_reading

    def close(self):
        # No-op: the camera_broker owns camera handles.
        pass


# ── Occupancy Grid ───────────────────────────────────────────────────

GRID_RESOLUTION_CM = 10   # each cell = 10cm x 10cm
GRID_SIZE = 200           # 200x200 = 20m x 20m (plenty for a house)
GRID_ORIGIN = GRID_SIZE // 2  # robot starts at center

# Cell values
UNKNOWN = 0
FREE = 1
OCCUPIED = 2


class OccupancyGrid:
    """2D occupancy grid — the map commercial robots use.
    
    Each cell is 10cm x 10cm. Values:
        0 = unknown (haven't looked)
        1 = free (can drive through)  
        2 = occupied (wall/furniture/obstacle)
    
    Updated from stereo depth readings + dead-reckoning position.
    Enables real path planning (A* to a frontier cell).
    """

    def __init__(self):
        self.grid = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.uint8)
        self.resolution = GRID_RESOLUTION_CM
        self.size = GRID_SIZE
        self.origin = GRID_ORIGIN
        self._save_path = Path.home() / ".repryntt" / "brain" / "occupancy_grid.npz"
        self._load()

    def _load(self):
        """Load grid from disk if exists."""
        if self._save_path.exists():
            try:
                data = np.load(str(self._save_path))
                self.grid = data["grid"]
                logger.info(f"Loaded occupancy grid: {np.sum(self.grid == FREE)} free, "
                            f"{np.sum(self.grid == OCCUPIED)} occupied cells")
            except Exception as e:
                logger.debug(f"Failed to load occupancy grid: {e}")

    def save(self):
        """Persist grid to disk."""
        try:
            self._save_path.parent.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(str(self._save_path), grid=self.grid)
        except Exception as e:
            logger.debug(f"Failed to save occupancy grid: {e}")

    def world_to_grid(self, x_cm: float, y_cm: float) -> Tuple[int, int]:
        """Convert world coordinates (cm) to grid indices."""
        gx = int(round(x_cm / self.resolution)) + self.origin
        gy = int(round(y_cm / self.resolution)) + self.origin
        gx = max(0, min(self.size - 1, gx))
        gy = max(0, min(self.size - 1, gy))
        return gx, gy

    def update_from_reading(self, robot_x: float, robot_y: float,
                            heading_deg: float, reading: ObstacleReading):
        """Update grid from a stereo depth reading at the robot's position.
        
        Casts rays from robot position in left/center/right directions.
        Marks cells along the ray as FREE, and the endpoint as OCCUPIED
        if the zone had high proximity.
        """
        heading_rad = math.radians(heading_deg)

        # Three zones: -30°, 0°, +30° relative to heading
        zone_angles = {
            "left": heading_rad - math.radians(30),
            "center": heading_rad,
            "right": heading_rad + math.radians(30),
        }
        zone_readings = {
            "left": reading.left,
            "center": reading.center,
            "right": reading.right,
        }

        for zone_name, angle in zone_angles.items():
            proximity = zone_readings[zone_name]

            # Estimate distance for this zone
            if proximity > 0.5:
                # Close obstacle — distance = rough estimate from proximity
                ray_dist_cm = max(20, (1.0 - proximity) * 150)
            else:
                # Clear — mark as free up to ~150cm
                ray_dist_cm = 150

            # Ray-march from robot to endpoint
            steps = int(ray_dist_cm / self.resolution) + 1
            for step in range(steps):
                dist = step * self.resolution
                cx = robot_x + dist * math.sin(angle)
                cy = robot_y + dist * math.cos(angle)
                gx, gy = self.world_to_grid(cx, cy)

                if step < steps - 1 or proximity < 0.4:
                    # Along the ray or endpoint is clear
                    self.grid[gy, gx] = FREE
                else:
                    # Endpoint of a blocked ray
                    if proximity > 0.4:
                        self.grid[gy, gx] = OCCUPIED

        self.save()

    def get_frontier_cells(self, robot_x: float, robot_y: float,
                           max_count: int = 10) -> list:
        """Find frontier cells — free cells adjacent to unknown cells.
        
        These are the edges of explored territory. Going there expands the map.
        Returns list of (x_cm, y_cm, distance_from_robot) sorted by distance.
        """
        frontiers = []
        free_mask = self.grid == FREE
        unknown_mask = self.grid == UNKNOWN

        # Find free cells next to unknown cells using dilation
        kernel = np.ones((3, 3), np.uint8)
        if CV2_AVAILABLE:
            dilated_unknown = cv2.dilate(unknown_mask.astype(np.uint8), kernel) > 0
        else:
            # Manual dilation fallback
            dilated_unknown = np.zeros_like(unknown_mask)
            for dy in [-1, 0, 1]:
                for dx in [-1, 0, 1]:
                    shifted = np.roll(np.roll(unknown_mask, dy, axis=0), dx, axis=1)
                    dilated_unknown |= shifted

        frontier_mask = free_mask & dilated_unknown

        ys, xs = np.where(frontier_mask)
        if len(xs) == 0:
            return []

        # Convert to world coordinates and sort by distance
        rgx, rgy = self.world_to_grid(robot_x, robot_y)
        for gx, gy in zip(xs, ys):
            wx = (gx - self.origin) * self.resolution
            wy = (gy - self.origin) * self.resolution
            dist = math.sqrt((wx - robot_x)**2 + (wy - robot_y)**2)
            frontiers.append((float(wx), float(wy), float(dist)))

        # Sort by distance, return nearest
        frontiers.sort(key=lambda f: f[2])
        return frontiers[:max_count]

    def get_summary(self) -> Dict[str, Any]:
        """Summary stats for the occupancy grid."""
        total = self.size * self.size
        free = int(np.sum(self.grid == FREE))
        occupied = int(np.sum(self.grid == OCCUPIED))
        unknown = total - free - occupied
        return {
            "resolution_cm": self.resolution,
            "grid_size": self.size,
            "free_cells": free,
            "occupied_cells": occupied,
            "unknown_cells": unknown,
            "explored_pct": round((free + occupied) / total * 100, 2),
            "free_area_sqm": round(free * (self.resolution / 100) ** 2, 2),
        }

    def reset(self):
        """Clear the grid."""
        self.grid = np.zeros((GRID_SIZE, GRID_SIZE), dtype=np.uint8)
        self.save()


# ── Singletons ───────────────────────────────────────────────────────

_detector: Optional[LocalObstacleDetector] = None
_grid: Optional[OccupancyGrid] = None


def get_obstacle_detector() -> LocalObstacleDetector:
    global _detector
    if _detector is None:
        _detector = LocalObstacleDetector()
    return _detector


def get_occupancy_grid() -> OccupancyGrid:
    global _grid
    if _grid is None:
        _grid = OccupancyGrid()
    return _grid
