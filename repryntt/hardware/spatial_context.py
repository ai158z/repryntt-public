"""
repryntt.hardware.spatial_context — Build pose/map/frontier text for the VLM.

This module exists so the VLM stops treating every frame as frame-zero. It
turns the existing `SpatialMap` (dead-reckoning pose) and `OccupancyGrid`
(stereo-depth-built map) into a short text block that the VLM reads BEFORE
it looks at the image:

    "You are at (269, -530) cm, heading 117° (east-southeast). You have
    travelled 24.4 m in 539 moves. Nearest unknown frontier: 1.4 m ahead-
    right (heading delta +23°). You have visited 108 places; 89 frontiers
    are still unexplored."

Same data the explorer already tracks — just surfaced. No new sensors.
When the IMU arrives, replace the dead-reckoning estimates with fused
odometry and this module keeps working unchanged.
"""

from __future__ import annotations

import math
from typing import List, Optional, Tuple


def _compass(heading_deg: float) -> str:
    """Snap heading to an 8-way compass name."""
    names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = int(((heading_deg % 360) + 22.5) / 45) % 8
    return names[idx]


def _bearing_to(robot_x: float, robot_y: float,
                robot_heading_deg: float,
                target_x: float, target_y: float) -> Tuple[float, float]:
    """Return (distance_cm, bearing_delta_deg) from robot to target.

    bearing_delta is signed: positive = target is to the robot's RIGHT,
    negative = LEFT. Zero means dead ahead.
    """
    dx = target_x - robot_x
    dy = target_y - robot_y
    dist = math.hypot(dx, dy)
    # World bearing: 0° = +Y (north), clockwise. Matches SpatialMap
    # convention where x = cm east, y = cm north, heading 0 = north.
    world_bearing = math.degrees(math.atan2(dx, dy)) % 360
    delta = (world_bearing - robot_heading_deg + 540) % 360 - 180
    return dist, delta


def _bearing_phrase(delta_deg: float) -> str:
    """Turn a signed bearing delta into human text."""
    a = abs(delta_deg)
    if a < 15:
        return "dead ahead"
    side = "right" if delta_deg > 0 else "left"
    if a < 45:
        return f"slightly {side} ({a:.0f}°)"
    if a < 80:
        return f"{side} ({a:.0f}°)"
    if a < 110:
        return f"directly {side} ({a:.0f}°)"
    if a < 160:
        return f"behind-{side} ({a:.0f}°)"
    return "behind you"


def build_spatial_context(max_frontiers: int = 3) -> str:
    """Compose a short pose + map + frontier text block for the VLM prompt.

    Returns empty string if spatial system isn't available (safe to call
    unconditionally). The text is intended to be prepended to the nav
    prompt on every perceive() call.
    """
    try:
        from repryntt.hardware.spatial_map import get_spatial_map
        from repryntt.hardware.local_perception import (
            OccupancyGrid, FREE, OCCUPIED, UNKNOWN,
        )
    except Exception:
        return ""

    try:
        smap = get_spatial_map()
    except Exception:
        return ""

    x = float(getattr(smap, "x", 0.0))
    y = float(getattr(smap, "y", 0.0))
    heading = float(getattr(smap, "heading", 0.0))
    moves = int(getattr(smap, "move_count", 0))
    total_cm = float(getattr(smap, "total_distance_cm", 0.0))
    place_count = len(getattr(smap, "places", {}) or {})

    lines: List[str] = []
    lines.append("YOU ARE A TANK-ROBOT WITH SPATIAL MEMORY.")
    lines.append(
        f"Pose: ({x:+.0f}, {y:+.0f}) cm, heading {heading:.0f}° "
        f"({_compass(heading)}). Travelled {total_cm/100:.1f} m in "
        f"{moves} moves. Visited {place_count} places so far."
    )

    # Try to pull frontier cells from the occupancy grid.
    frontier_lines: List[str] = []
    try:
        grid = OccupancyGrid()
        free = int((grid.grid == FREE).sum())
        occ = int((grid.grid == OCCUPIED).sum())
        unk = int((grid.grid == UNKNOWN).sum())
        lines.append(
            f"Map: {free} free cells, {occ} occupied, {unk} unknown "
            f"(each cell = 10 cm)."
        )
        cells = grid.get_frontier_cells(x, y, max_count=max_frontiers * 2)
        # get_frontier_cells returns (wx, wy, dist) tuples sorted by distance.
        for item in cells[:max_frontiers]:
            if len(item) >= 3:
                fx, fy, _d = item[0], item[1], item[2]
                dist_cm, delta = _bearing_to(x, y, heading, fx, fy)
                frontier_lines.append(
                    f"  • unknown territory at ({fx:+.0f}, {fy:+.0f}) cm — "
                    f"{dist_cm/100:.1f} m {_bearing_phrase(delta)}"
                )
    except Exception:
        pass

    if frontier_lines:
        lines.append("Nearest unexplored frontiers (grid-based):")
        lines.extend(frontier_lines)

    # Also surface semantic frontiers the VLM has logged (e.g. "open door
    # to the left I didn't enter"). Different from grid frontiers — these
    # are VLM-reported open paths Andrew hasn't followed yet.
    sem_frontiers = getattr(smap, "frontiers", []) or []
    unexplored = [f for f in sem_frontiers
                  if isinstance(f, dict) and not f.get("explored")]
    if unexplored:
        lines.append(
            f"Unexplored semantic frontiers ({len(unexplored)} total, "
            f"showing last {min(3, len(unexplored))}):"
        )
        for f in unexplored[-3:]:
            d = f.get("direction", "?")
            desc = f.get("description", "")
            lines.append(f"  • {d}: {desc[:80]}")

    lines.append(
        "USE THIS. Don't guess — if a frontier is to your right, picking "
        "turn_right actually takes you toward unexplored territory."
    )
    return "\n".join(lines)


def frontier_bias_direction() -> Optional[str]:
    """Return a preferred direction ('forward'/'turn_left'/'turn_right') toward
    the nearest grid frontier, or None if no frontier is available.

    Used as a soft tiebreaker when the VLM is uncertain.
    """
    try:
        from repryntt.hardware.spatial_map import get_spatial_map
        from repryntt.hardware.local_perception import OccupancyGrid
    except Exception:
        return None
    try:
        smap = get_spatial_map()
        grid = OccupancyGrid()
        cells = grid.get_frontier_cells(
            float(smap.x), float(smap.y), max_count=1)
        if not cells:
            return None
        fx, fy = cells[0][0], cells[0][1]
        _dist, delta = _bearing_to(
            float(smap.x), float(smap.y), float(smap.heading), fx, fy)
        if abs(delta) < 25:
            return "forward"
        return "turn_right" if delta > 0 else "turn_left"
    except Exception:
        return None
