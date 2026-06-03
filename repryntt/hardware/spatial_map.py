"""
repryntt.hardware.spatial_map — Persistent Spatial Map for Navigation.

Tracks where Andrew has been, what he saw, and where he HASN'T gone yet.
This gives him continuity across heartbeats — an internal map he can
reason about to decide where to explore next.

Architecture:
    - Dead-reckoning position from motor commands (no GPS/SLAM)
    - Places: named nodes with scene descriptions and connections
    - Rooms: classified areas (kitchen, hallway, etc.) from VLM data
    - Landmarks: notable objects/features as graph nodes
    - Frontiers: directions seen but not yet explored
    - A* path planning on the occupancy grid
    - Indoor/outdoor detection from scene classification history

The map is a hybrid semantic-metric graph:
    - Semantic layer: places, rooms, landmarks (LLM-friendly)
    - Metric layer: occupancy grid + A* (robot-friendly)
    - Both persist to disk and survive daemon restarts
"""

from __future__ import annotations

import heapq
import json
import logging
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MAP_FILE = Path.home() / ".repryntt" / "brain" / "spatial_map.json"

HEADING_NAMES = {
    0: "north", 45: "northeast", 90: "east", 135: "southeast",
    180: "south", 225: "southwest", 270: "west", 315: "northwest",
}

INDOOR_SCENE_TYPES = frozenset({
    "hallway", "corridor", "room", "kitchen", "bathroom", "bedroom",
    "living_room", "garage", "doorway", "stairs", "elevator",
    "closet", "storage", "office", "laundry", "dining_room", "basement",
    "attic", "lobby", "foyer",
})

OUTDOOR_SCENE_TYPES = frozenset({
    "outdoor", "patio", "yard", "driveway", "sidewalk", "street",
    "parking_lot", "garden", "park", "trail", "field",
})

TRANSITION_SCENE_TYPES = frozenset({
    "doorway", "entrance", "exit", "threshold", "porch",
})

PLACE_MERGE_RADIUS_CM = 50
ROOM_MERGE_RADIUS_CM = 150
LANDMARK_MERGE_RADIUS_CM = 80


def _heading_name(degrees: float) -> str:
    """Snap heading to nearest compass name."""
    normalized = degrees % 360
    closest = min(HEADING_NAMES.keys(), key=lambda k: min(
        abs(k - normalized), 360 - abs(k - normalized)))
    return HEADING_NAMES[closest]


def _distance(x1: float, y1: float, x2: float, y2: float) -> float:
    return math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)


def _heading_between(x1: float, y1: float, x2: float, y2: float) -> float:
    """Heading in degrees from (x1,y1) toward (x2,y2). 0=north(+y)."""
    return math.degrees(math.atan2(x2 - x1, y2 - y1)) % 360


# ── A* Path Planner ──────────────────────────────────────────────────

@dataclass
class PathResult:
    """Result of A* path planning."""
    success: bool
    path: List[Tuple[float, float]] = field(default_factory=list)
    distance_cm: float = 0.0
    waypoints: int = 0
    heading_to_first: float = 0.0
    direction_name: str = ""
    blocked_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "waypoints": self.waypoints,
            "distance_cm": round(self.distance_cm, 1),
            "heading_to_first": round(self.heading_to_first, 1),
            "direction_name": self.direction_name,
            "blocked_reason": self.blocked_reason,
            "path_points": [(round(x, 1), round(y, 1))
                            for x, y in self.path[:20]],
        }


def plan_path_astar(grid, resolution: int, origin: int,
                    start_cm: Tuple[float, float],
                    goal_cm: Tuple[float, float],
                    occupied_value: int = 2) -> PathResult:
    """A* path planning on the occupancy grid.

    Works on the numpy occupancy grid from local_perception.py.
    Returns a PathResult with waypoints in world coordinates (cm).

    Uses 8-connected neighbors with diagonal cost sqrt(2).
    Treats UNKNOWN cells as passable (optimistic planning).
    """
    import numpy as np

    def to_grid(x: float, y: float) -> Tuple[int, int]:
        gx = int(round(x / resolution)) + origin
        gy = int(round(y / resolution)) + origin
        return (max(0, min(grid.shape[1] - 1, gx)),
                max(0, min(grid.shape[0] - 1, gy)))

    def to_world(gx: int, gy: int) -> Tuple[float, float]:
        return ((gx - origin) * resolution, (gy - origin) * resolution)

    start_g = to_grid(*start_cm)
    goal_g = to_grid(*goal_cm)

    if grid[goal_g[1], goal_g[0]] == occupied_value:
        # Goal is blocked — find nearest free cell
        best_g = None
        best_dist = float('inf')
        for dy in range(-3, 4):
            for dx in range(-3, 4):
                ny, nx = goal_g[1] + dy, goal_g[0] + dx
                if (0 <= ny < grid.shape[0] and 0 <= nx < grid.shape[1]
                        and grid[ny, nx] != occupied_value):
                    d = abs(dx) + abs(dy)
                    if d < best_dist:
                        best_dist = d
                        best_g = (nx, ny)
        if best_g:
            goal_g = best_g
        else:
            return PathResult(success=False,
                              blocked_reason="Goal area is fully blocked")

    SQRT2 = 1.414
    neighbors = [(-1, 0, 1), (1, 0, 1), (0, -1, 1), (0, 1, 1),
                 (-1, -1, SQRT2), (-1, 1, SQRT2), (1, -1, SQRT2), (1, 1, SQRT2)]

    open_set: list = []
    heapq.heappush(open_set, (0.0, start_g))
    came_from: Dict[Tuple[int, int], Tuple[int, int]] = {}
    g_score: Dict[Tuple[int, int], float] = {start_g: 0.0}

    def heuristic(a: Tuple[int, int], b: Tuple[int, int]) -> float:
        return math.sqrt((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2)

    max_iterations = 50000

    for _ in range(max_iterations):
        if not open_set:
            return PathResult(success=False,
                              blocked_reason="No path found — area unreachable")

        _, current = heapq.heappop(open_set)

        if current == goal_g:
            # Reconstruct path
            path_grid = [current]
            while current in came_from:
                current = came_from[current]
                path_grid.append(current)
            path_grid.reverse()

            # Convert to world coords, simplify (skip every other point)
            path_world = [to_world(gx, gy) for gx, gy in path_grid]
            simplified = _simplify_path(path_world, tolerance=resolution * 1.5)

            total_dist = sum(
                _distance(*simplified[i], *simplified[i + 1])
                for i in range(len(simplified) - 1)
            )

            heading = 0.0
            direction = "forward"
            if len(simplified) >= 2:
                heading = _heading_between(
                    simplified[0][0], simplified[0][1],
                    simplified[1][0], simplified[1][1])
                direction = _heading_name(heading)

            return PathResult(
                success=True,
                path=simplified,
                distance_cm=total_dist,
                waypoints=len(simplified),
                heading_to_first=heading,
                direction_name=direction,
            )

        for dx, dy, cost in neighbors:
            nx, ny = current[0] + dx, current[1] + dy
            if not (0 <= nx < grid.shape[1] and 0 <= ny < grid.shape[0]):
                continue
            if grid[ny, nx] == occupied_value:
                continue

            neighbor = (nx, ny)
            tentative = g_score[current] + cost

            if tentative < g_score.get(neighbor, float('inf')):
                came_from[neighbor] = current
                g_score[neighbor] = tentative
                f = tentative + heuristic(neighbor, goal_g)
                heapq.heappush(open_set, (f, neighbor))

    return PathResult(success=False,
                      blocked_reason="Search exceeded iteration limit")


def _simplify_path(path: List[Tuple[float, float]],
                   tolerance: float) -> List[Tuple[float, float]]:
    """Ramer-Douglas-Peucker path simplification."""
    if len(path) <= 2:
        return path

    # Find point with max distance from line (start→end)
    start, end = path[0], path[-1]
    max_dist = 0.0
    max_idx = 0
    line_len = _distance(*start, *end)
    if line_len < 1e-6:
        return [start, end]

    for i in range(1, len(path) - 1):
        # Point-to-line distance
        px, py = path[i]
        dx, dy = end[0] - start[0], end[1] - start[1]
        t = max(0, min(1, ((px - start[0]) * dx + (py - start[1]) * dy) / (line_len ** 2)))
        proj_x = start[0] + t * dx
        proj_y = start[1] + t * dy
        d = _distance(px, py, proj_x, proj_y)
        if d > max_dist:
            max_dist = d
            max_idx = i

    if max_dist > tolerance:
        left = _simplify_path(path[:max_idx + 1], tolerance)
        right = _simplify_path(path[max_idx:], tolerance)
        return left[:-1] + right
    else:
        return [start, end]


# ── Room Node ────────────────────────────────────────────────────────

@dataclass
class Room:
    """A classified spatial area (kitchen, hallway, etc.)."""
    room_id: str
    room_type: str         # kitchen, hallway, bedroom, outdoor, unknown
    center_x: float
    center_y: float
    scene_types_seen: List[str] = field(default_factory=list)
    descriptions: List[str] = field(default_factory=list)
    visit_count: int = 0
    first_visited: float = 0.0
    last_visited: float = 0.0
    is_indoor: Optional[bool] = None
    connected_rooms: List[str] = field(default_factory=list)

    def classify(self) -> str:
        """Re-classify room type from accumulated scene observations."""
        if not self.scene_types_seen:
            return self.room_type
        counts = Counter(self.scene_types_seen)
        most_common = counts.most_common(1)[0][0]
        if most_common != "unknown":
            self.room_type = most_common
        return self.room_type

    def to_dict(self) -> Dict[str, Any]:
        return {
            "room_id": self.room_id,
            "room_type": self.room_type,
            "center_x": round(self.center_x, 1),
            "center_y": round(self.center_y, 1),
            "scene_types_seen": self.scene_types_seen[-20:],
            "descriptions": self.descriptions[-5:],
            "visit_count": self.visit_count,
            "first_visited": self.first_visited,
            "last_visited": self.last_visited,
            "is_indoor": self.is_indoor,
            "connected_rooms": self.connected_rooms,
        }

    @staticmethod
    def from_dict(data: Dict) -> "Room":
        return Room(
            room_id=data["room_id"],
            room_type=data.get("room_type", "unknown"),
            center_x=data.get("center_x", 0),
            center_y=data.get("center_y", 0),
            scene_types_seen=data.get("scene_types_seen", []),
            descriptions=data.get("descriptions", []),
            visit_count=data.get("visit_count", 0),
            first_visited=data.get("first_visited", 0),
            last_visited=data.get("last_visited", 0),
            is_indoor=data.get("is_indoor"),
            connected_rooms=data.get("connected_rooms", []),
        )


# ── Landmark Node ────────────────────────────────────────────────────

@dataclass
class Landmark:
    """A notable spatial feature — doorway, furniture, sign, etc."""
    landmark_id: str
    description: str
    landmark_type: str     # doorway, furniture, sign, window, staircase, etc
    x: float
    y: float
    heading_seen: float
    first_seen: float = 0.0
    times_seen: int = 1
    in_room: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "landmark_id": self.landmark_id,
            "description": self.description[:200],
            "landmark_type": self.landmark_type,
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "heading_seen": round(self.heading_seen, 1),
            "first_seen": self.first_seen,
            "times_seen": self.times_seen,
            "in_room": self.in_room,
        }

    @staticmethod
    def from_dict(data: Dict) -> "Landmark":
        return Landmark(
            landmark_id=data["landmark_id"],
            description=data.get("description", ""),
            landmark_type=data.get("landmark_type", "unknown"),
            x=data.get("x", 0),
            y=data.get("y", 0),
            heading_seen=data.get("heading_seen", 0),
            first_seen=data.get("first_seen", 0),
            times_seen=data.get("times_seen", 1),
            in_room=data.get("in_room"),
        )


# ── Spatial Map ──────────────────────────────────────────────────────

class SpatialMap:
    """Persistent spatial map for LLM-driven exploration.

    Hybrid semantic-metric graph:
        Semantic: places, rooms, landmarks (Andrew reasons about these)
        Metric: occupancy grid + A* path planning (body navigates with these)

    Tracks:
        - Current position (x, y) and heading (degrees) via dead reckoning
        - Places visited: semantic nodes with descriptions
        - Rooms: classified areas from VLM scene_type data
        - Landmarks: notable objects as graph nodes
        - Frontiers: open paths not yet explored
        - Indoor/outdoor state from scene history
        - Connections between places and rooms (graph edges)
    """

    def __init__(self):
        self.x: float = 0.0
        self.y: float = 0.0
        self.heading: float = 0.0
        self.places: Dict[str, Dict] = {}
        self.rooms: Dict[str, Room] = {}
        self.landmarks: Dict[str, Landmark] = {}
        self.frontiers: List[Dict] = []
        self.move_count: int = 0
        self.total_distance_cm: float = 0.0
        self.started_at: float = time.time()
        self._current_room_id: Optional[str] = None
        self._environment: str = "unknown"  # indoor / outdoor / unknown
        self._env_history: List[str] = []
        self._load()

    # ── Persistence ──────────────────────────────────────────────

    def _load(self):
        """Load map from disk if it exists."""
        if MAP_FILE.exists():
            try:
                data = json.loads(MAP_FILE.read_text())
                self.x = data.get("x", 0.0)
                self.y = data.get("y", 0.0)
                self.heading = data.get("heading", 0.0)
                self.places = data.get("places", {})
                self.frontiers = data.get("frontiers", [])
                self.move_count = data.get("move_count", 0)
                self.total_distance_cm = data.get("total_distance_cm", 0.0)
                self.started_at = data.get("started_at", time.time())
                self._current_room_id = data.get("current_room_id")
                self._environment = data.get("environment", "unknown")
                self._env_history = data.get("env_history", [])

                for rid, rdata in data.get("rooms", {}).items():
                    self.rooms[rid] = Room.from_dict(rdata)
                for lid, ldata in data.get("landmarks", {}).items():
                    self.landmarks[lid] = Landmark.from_dict(ldata)

                logger.info(
                    f"Loaded spatial map: {len(self.places)} places, "
                    f"{len(self.rooms)} rooms, {len(self.landmarks)} landmarks, "
                    f"{len(self.frontiers)} frontiers"
                )
            except Exception as e:
                logger.warning(f"Failed to load spatial map: {e}")

    def save(self):
        """Persist map to disk."""
        data = {
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "heading": round(self.heading, 1),
            "places": self.places,
            "rooms": {rid: r.to_dict() for rid, r in self.rooms.items()},
            "landmarks": {lid: l.to_dict() for lid, l in self.landmarks.items()},
            "frontiers": self.frontiers,
            "move_count": self.move_count,
            "total_distance_cm": round(self.total_distance_cm, 1),
            "started_at": self.started_at,
            "updated_at": time.time(),
            "current_room_id": self._current_room_id,
            "environment": self._environment,
            "env_history": self._env_history[-50:],
        }
        try:
            MAP_FILE.parent.mkdir(parents=True, exist_ok=True)
            MAP_FILE.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning(f"Failed to save spatial map: {e}")

    # ── Movement Tracking ────────────────────────────────────────

    def record_move(self, action: str, speed: float, duration: float,
                    scene: str = "", obstacles: Optional[Dict] = None):
        """Update position estimate after a motor command."""
        max_cm_per_sec = 50.0
        distance_cm = speed * duration * max_cm_per_sec

        if action == "forward":
            rad = math.radians(self.heading)
            self.x += distance_cm * math.sin(rad)
            self.y += distance_cm * math.cos(rad)
            self.total_distance_cm += distance_cm
        elif action == "backward":
            rad = math.radians(self.heading)
            self.x -= distance_cm * math.sin(rad)
            self.y -= distance_cm * math.cos(rad)
            self.total_distance_cm += distance_cm
        elif action == "turn_left":
            turn_degrees = speed * duration * 150
            self.heading = (self.heading - turn_degrees) % 360
        elif action == "turn_right":
            turn_degrees = speed * duration * 150
            self.heading = (self.heading + turn_degrees) % 360

        self.move_count += 1

        if self.move_count % 5 == 0 and scene:
            place_id = f"place_{len(self.places) + 1}"
            self._register_place(place_id, scene)

        self.save()

    # ── Observation Recording ────────────────────────────────────

    def record_observation(self, scene: str, obstacles: Optional[Dict] = None,
                           best_direction: str = "",
                           frontiers_seen: Optional[List[str]] = None,
                           scene_type: str = ""):
        """Record what Andrew sees at the current position.

        Extended to support room classification, indoor/outdoor detection,
        and landmark extraction from scene descriptions.
        """
        nearest = self._nearest_place(max_dist=PLACE_MERGE_RADIUS_CM)
        if nearest:
            self.places[nearest]["last_seen"] = scene[:200]
            self.places[nearest]["visit_count"] = \
                self.places[nearest].get("visit_count", 0) + 1
            self.places[nearest]["last_visited"] = time.time()
        else:
            place_id = f"place_{len(self.places) + 1}"
            self._register_place(place_id, scene)

        if frontiers_seen:
            for direction in frontiers_seen:
                self._add_frontier(direction, scene)

        if obstacles:
            for direction in ["left", "right"]:
                if (obstacles.get(direction, 1.0) < 0.3
                        and direction != best_direction):
                    self._add_frontier(direction, f"Open path to {direction}")

        if scene_type:
            self._update_room(scene_type, scene)
            self._update_environment(scene_type)

        self.save()

    def record_landmark(self, description: str, landmark_type: str = "object"):
        """Record a notable landmark at the current position.

        Called when the VLM identifies something distinctive: a doorway,
        piece of furniture, sign, window, staircase, etc.
        """
        nearby = self._nearest_landmark(max_dist=LANDMARK_MERGE_RADIUS_CM)
        if nearby:
            lm = self.landmarks[nearby]
            lm.times_seen += 1
            lm.description = description[:200]
            return

        lid = f"lm_{len(self.landmarks) + 1}"
        lm = Landmark(
            landmark_id=lid,
            description=description[:200],
            landmark_type=landmark_type,
            x=self.x,
            y=self.y,
            heading_seen=self.heading,
            first_seen=time.time(),
            in_room=self._current_room_id,
        )
        self.landmarks[lid] = lm
        logger.debug(f"New landmark: {lid} = {description[:60]}")
        self.save()

    # ── Room Classification ──────────────────────────────────────

    def _update_room(self, scene_type: str, scene_desc: str = ""):
        """Update or create a room node from VLM scene classification."""
        if not scene_type or scene_type == "unknown":
            return

        nearest_room = self._nearest_room(max_dist=ROOM_MERGE_RADIUS_CM)
        now = time.time()

        if nearest_room:
            room = self.rooms[nearest_room]
            room.scene_types_seen.append(scene_type)
            room.scene_types_seen = room.scene_types_seen[-30:]
            if scene_desc:
                room.descriptions.append(scene_desc[:150])
                room.descriptions = room.descriptions[-5:]
            room.visit_count += 1
            room.last_visited = now
            room.classify()

            # Track room transitions
            if (self._current_room_id and
                    self._current_room_id != nearest_room):
                old_room = self.rooms.get(self._current_room_id)
                if old_room and nearest_room not in old_room.connected_rooms:
                    old_room.connected_rooms.append(nearest_room)
                if nearest_room not in room.connected_rooms and self._current_room_id:
                    room.connected_rooms.append(self._current_room_id)

            self._current_room_id = nearest_room
        else:
            rid = f"room_{len(self.rooms) + 1}"
            is_indoor = None
            if scene_type in INDOOR_SCENE_TYPES:
                is_indoor = True
            elif scene_type in OUTDOOR_SCENE_TYPES:
                is_indoor = False

            room = Room(
                room_id=rid,
                room_type=scene_type,
                center_x=self.x,
                center_y=self.y,
                scene_types_seen=[scene_type],
                descriptions=[scene_desc[:150]] if scene_desc else [],
                visit_count=1,
                first_visited=now,
                last_visited=now,
                is_indoor=is_indoor,
            )

            if self._current_room_id and self._current_room_id in self.rooms:
                room.connected_rooms.append(self._current_room_id)
                self.rooms[self._current_room_id].connected_rooms.append(rid)

            self.rooms[rid] = room
            self._current_room_id = rid
            logger.info(f"New room: {rid} = {scene_type}")

    def _update_environment(self, scene_type: str):
        """Track indoor/outdoor state from recent scene classifications."""
        if scene_type in INDOOR_SCENE_TYPES:
            self._env_history.append("indoor")
        elif scene_type in OUTDOOR_SCENE_TYPES:
            self._env_history.append("outdoor")
        elif scene_type in TRANSITION_SCENE_TYPES:
            self._env_history.append("transition")

        self._env_history = self._env_history[-20:]

        if len(self._env_history) >= 3:
            recent = self._env_history[-5:]
            indoor_count = recent.count("indoor")
            outdoor_count = recent.count("outdoor")
            if indoor_count > outdoor_count:
                self._environment = "indoor"
            elif outdoor_count > indoor_count:
                self._environment = "outdoor"

    # ── Path Planning ────────────────────────────────────────────

    def plan_path_to(self, goal_x: float, goal_y: float) -> PathResult:
        """Plan A* path from current position to a goal (world coords, cm).

        Uses the occupancy grid from local_perception.py.
        Returns PathResult with waypoints and heading to first waypoint.
        """
        try:
            from repryntt.hardware.local_perception import get_occupancy_grid
            grid_obj = get_occupancy_grid()
            return plan_path_astar(
                grid=grid_obj.grid,
                resolution=grid_obj.resolution,
                origin=grid_obj.origin,
                start_cm=(self.x, self.y),
                goal_cm=(goal_x, goal_y),
            )
        except Exception as e:
            logger.warning(f"Path planning failed: {e}")
            # Fallback: straight-line heading
            heading = _heading_between(self.x, self.y, goal_x, goal_y)
            dist = _distance(self.x, self.y, goal_x, goal_y)
            return PathResult(
                success=True,
                path=[(self.x, self.y), (goal_x, goal_y)],
                distance_cm=dist,
                waypoints=2,
                heading_to_first=heading,
                direction_name=_heading_name(heading),
                blocked_reason="straight-line fallback (no grid)",
            )

    def plan_path_to_room(self, room_id: str) -> PathResult:
        """Plan path to the center of a known room."""
        room = self.rooms.get(room_id)
        if not room:
            return PathResult(success=False,
                              blocked_reason=f"Unknown room: {room_id}")
        return self.plan_path_to(room.center_x, room.center_y)

    def plan_path_to_landmark(self, landmark_id: str) -> PathResult:
        """Plan path to a known landmark."""
        lm = self.landmarks.get(landmark_id)
        if not lm:
            return PathResult(success=False,
                              blocked_reason=f"Unknown landmark: {landmark_id}")
        return self.plan_path_to(lm.x, lm.y)

    def find_room_by_type(self, room_type: str) -> Optional[Room]:
        """Find the nearest room of a given type (e.g., 'kitchen')."""
        best = None
        best_dist = float('inf')
        for room in self.rooms.values():
            if room.room_type == room_type:
                d = _distance(self.x, self.y, room.center_x, room.center_y)
                if d < best_dist:
                    best_dist = d
                    best = room
        return best

    # ── Context Generation ───────────────────────────────────────

    def resolve_frontier(self, frontier_description: str):
        """Mark a frontier as explored when Andrew goes there."""
        self.frontiers = [
            f for f in self.frontiers
            if frontier_description.lower() not in f.get("description", "").lower()
        ]
        self.save()

    def get_exploration_context(self) -> str:
        """Generate natural language summary for heartbeat prompt.

        This is what Andrew reads each heartbeat to know where he's been,
        what rooms he's discovered, and where to explore next.
        """
        lines = []
        lines.append("## YOUR SPATIAL MAP")
        lines.append(
            f"Position: ({self.x:.0f}, {self.y:.0f}) cm from start, "
            f"facing {_heading_name(self.heading)} ({self.heading:.0f}deg)")
        lines.append(
            f"Moves: {self.move_count} | Distance: {self.total_distance_cm:.0f} cm | "
            f"Environment: {self._environment}")

        if self._current_room_id and self._current_room_id in self.rooms:
            room = self.rooms[self._current_room_id]
            lines.append(f"Current room: {room.room_type} ({room.room_id})")

        # Rooms
        if self.rooms:
            lines.append(f"\n**Rooms discovered ({len(self.rooms)}):**")
            for room in sorted(self.rooms.values(),
                               key=lambda r: r.visit_count, reverse=True):
                conns = ""
                if room.connected_rooms:
                    conn_types = []
                    for cid in room.connected_rooms[:3]:
                        cr = self.rooms.get(cid)
                        if cr:
                            conn_types.append(cr.room_type)
                    conns = f" [connects to: {', '.join(conn_types)}]"
                lines.append(
                    f"  - {room.room_id}: {room.room_type} "
                    f"({room.visit_count} visits, "
                    f"{'indoor' if room.is_indoor else 'outdoor' if room.is_indoor is False else '?'})"
                    f"{conns}")

        # Landmarks
        if self.landmarks:
            lines.append(f"\n**Landmarks ({len(self.landmarks)}):**")
            for lm in sorted(self.landmarks.values(),
                             key=lambda l: l.times_seen, reverse=True)[:10]:
                dist = _distance(self.x, self.y, lm.x, lm.y)
                direction = _heading_name(_heading_between(
                    self.x, self.y, lm.x, lm.y))
                lines.append(
                    f"  - {lm.description[:60]} "
                    f"({dist:.0f}cm {direction}, seen {lm.times_seen}x)")

        # Places
        if self.places:
            lines.append(f"\n**Places ({len(self.places)}):**")
            sorted_places = sorted(
                self.places.items(),
                key=lambda x: x[1].get("visit_count", 0),
                reverse=True)
            for pid, place in sorted_places[:8]:
                visits = place.get("visit_count", 1)
                desc = place.get("last_seen", place.get("description", ""))[:80]
                lines.append(f"  - {pid} ({visits} visits): {desc}")

        # Frontiers
        if self.frontiers:
            lines.append(
                f"\n**UNEXPLORED FRONTIERS ({len(self.frontiers)}) — GO HERE:**")
            for f in self.frontiers[:5]:
                lines.append(
                    f"  -> {f['direction']} from ({f['from_x']:.0f}, "
                    f"{f['from_y']:.0f}): {f['description'][:80]}")
            lines.append(
                "Move toward an unexplored frontier. Don't stay in the same area.")
        else:
            lines.append(
                "\n**No frontiers yet — look around with nav_look() "
                "and note open paths you haven't taken.**")

        # Staleness warning
        if self.places:
            most_visited = max(self.places.values(),
                               key=lambda p: p.get("visit_count", 0))
            if most_visited.get("visit_count", 0) > 5:
                lines.append(
                    f"\nWarning: You've visited "
                    f"{most_visited.get('description', 'one area')[:50]} "
                    f"{most_visited['visit_count']} times. MOVE SOMEWHERE NEW.")

        return "\n".join(lines)

    # ── Internal Helpers ─────────────────────────────────────────

    def _register_place(self, place_id: str, scene: str):
        self.places[place_id] = {
            "description": scene[:200],
            "last_seen": scene[:200],
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "heading": round(self.heading, 1),
            "discovered_at": time.time(),
            "last_visited": time.time(),
            "visit_count": 1,
        }

    def _nearest_place(self, max_dist: float = PLACE_MERGE_RADIUS_CM
                       ) -> Optional[str]:
        best_id = None
        best_dist = max_dist
        for pid, place in self.places.items():
            d = _distance(self.x, self.y, place["x"], place["y"])
            if d < best_dist:
                best_dist = d
                best_id = pid
        return best_id

    def _nearest_room(self, max_dist: float = ROOM_MERGE_RADIUS_CM
                      ) -> Optional[str]:
        best_id = None
        best_dist = max_dist
        for rid, room in self.rooms.items():
            d = _distance(self.x, self.y, room.center_x, room.center_y)
            if d < best_dist:
                best_dist = d
                best_id = rid
        return best_id

    def _nearest_landmark(self, max_dist: float = LANDMARK_MERGE_RADIUS_CM
                          ) -> Optional[str]:
        best_id = None
        best_dist = max_dist
        for lid, lm in self.landmarks.items():
            d = _distance(self.x, self.y, lm.x, lm.y)
            if d < best_dist:
                best_dist = d
                best_id = lid
        return best_id

    def _add_frontier(self, direction: str, description: str):
        for f in self.frontiers:
            if (abs(f["from_x"] - self.x) < 30
                    and abs(f["from_y"] - self.y) < 30
                    and f["direction"] == direction):
                return
        self.frontiers.append({
            "direction": direction,
            "description": description[:200],
            "from_x": round(self.x, 1),
            "from_y": round(self.y, 1),
            "from_heading": round(self.heading, 1),
            "discovered_at": time.time(),
        })

    def reset(self):
        """Clear the map and start fresh."""
        self.x = 0.0
        self.y = 0.0
        self.heading = 0.0
        self.places = {}
        self.rooms = {}
        self.landmarks = {}
        self.frontiers = []
        self.move_count = 0
        self.total_distance_cm = 0.0
        self._current_room_id = None
        self._environment = "unknown"
        self._env_history = []
        self.started_at = time.time()
        self.save()


# ── Singleton ────────────────────────────────────────────────────────

_spatial_map: Optional[SpatialMap] = None


def get_spatial_map() -> SpatialMap:
    """Get or create the singleton spatial map."""
    global _spatial_map
    if _spatial_map is None:
        _spatial_map = SpatialMap()
    return _spatial_map
