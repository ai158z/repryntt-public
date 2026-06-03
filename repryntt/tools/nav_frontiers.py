"""
repryntt.tools.nav_frontiers — Tool wrapper for frontier detection.

Wraps the spatial map's frontier computation in a tool interface that
returns JSON-serializable results. Handles int64/float64 serialization issues by
converting numpy integers and floats to native Python types before JSON encoding.

This fixed version resolves the JSON serialization bug where nav_frontiers() returned:
"Object of type int64 is not JSON serializable"
"""

import json
import logging
import traceback

import numpy as np

from repryntt.hardware.spatial_map import get_spatial_map
from repryntt.hardware.local_perception import OccupancyGrid
from repryntt.hardware.spatial_context import _bearing_to, _bearing_phrase

logger = logging.getLogger(__name__)


def convert_types_to_native(data):
    """Recursively convert numpy types and other non-JSON-serializable types to native Python types.

    Handles: np.int64, np.int32, np.float64, np.float32, np.int8, etc.
    Also handles numpy bool, bytes, and any numpy generic type.

    This function is the KEY to fixing the nav_frontiers JSON serialization bug.
    """
    # Handle numpy scalars
    if hasattr(data, 'dtype'):
        # Check if it's a numpy type
        type_str = str(type(data))
        if 'numpy' in type_str:
            try:
                # Convert numpy scalars to native Python
                if isinstance(data, (np.integer, np.floating, np.bool_)):
                    return data.item()  # .item() converts to native Python type
                # Handle numpy arrays with single elements
                elif isinstance(data, np.ndarray) and data.size == 1:
                    return data.item()
                # Handle other numpy types
                else:
                    return data
            except Exception:
                pass

    # Handle regular numpy types
    if isinstance(data, np.integer):
        return int(data)
    elif isinstance(data, np.floating):
        return float(data)
    elif isinstance(data, np.bool_):
        return bool(data)
    elif isinstance(data, dict):
        return {key: convert_types_to_native(value) for key, value in data.items()}
    elif isinstance(data, (list, tuple)):
        return [convert_types_to_native(item) for item in data]
    else:
        return data


def nav_frontiers(max_count: int = 5, **kw) -> str:
    """List the nearest unexplored frontier cells from the occupancy grid.

    Each frontier has world coords (cm), distance (m), and
    a bearing relative to the robot's current heading.

    Use these as concrete navigation goals instead of
    abstract terms like 'the hallway'.

    Returns:
    JSON string with robot_pose and frontiers array, or error JSON
    """
    try:
        smap = get_spatial_map()
        grid = OccupancyGrid()
        max_count = max(1, min(20, int(max_count)))

        # Get frontier cells from spatial memory
        # This returns numpy int64 values that must be converted
        cells_raw = grid.get_frontier_cells(
            float(smap.x), float(smap.y),
            max_count=max_count
        )

        logger.debug(f"get_frontiers returned {len(cells_raw)} cells")
        if cells_raw:
            logger.debug(f"First cell types: {[type(c).__name__ for c in cells_raw[0]]}")
            logger.debug(f"First cell values: {cells_raw[0]}")

        # Convert numpy types to native Python before building response
        items = []
        for c in cells_raw:
            # Each cell is a tuple: (world_x, world_y, distance, bearing)
            if len(c) >= 3:
                world_x, world_y, distance_m = c[0], c[1], c[2]
                # Convert numpy types to native Python to avoid JSON serialization errors
                world_x_native = convert_types_to_native(world_x)
                world_y_native = convert_types_to_native(world_y)
                distance_m_native = convert_types_to_native(distance_m)
                
                dist_cm, delta = _bearing_to(
                    float(smap.x), float(smap.y),
                    float(smap.heading),
                    float(convert_types_to_native(world_x_native)),
                    float(convert_types_to_native(world_y_native))
                )
                
                direction = _bearing_phrase(float(convert_types_to_native(delta)))
                items.append({
                    "world_x_cm": round(float(convert_types_to_native(world_x_native)), 1),
                    "world_y_cm": round(float(convert_types_to_native(world_y_native)), 1),
                    "distance_m": round(float(convert_types_to_native(distance_m_native)) / 100.0, 2),
                    "bearing_deg": round(float(convert_types_to_native(delta)), 1),
                    "direction": direction,
                })

        # Convert robot pose coordinates to native types
        robot_pose = {
            "x_cm": round(float(convert_types_to_native(smap.x)), 1),
            "y_cm": round(float(convert_types_to_native(smap.y)), 1),
            "heading_deg": round(float(convert_types_to_native(smap.heading)), 1),
            "compass": "unknown",
        }

        # Build compass direction from heading
        if hasattr(smap, 'heading'):
            robot_pose["compass"] = _bearing_phrase(float(convert_types_to_native(smap.heading)))

        result = {
            "robot_pose": robot_pose,
            "frontiers": items,
            "count": len(items),
        }

        return json.dumps(result)
        
    except Exception as e:
        error_trace = traceback.format_exc()
        logger.error(f"nav_frontiers() failed: {e}\n{error_trace}")
        return json.dumps({
            "error": str(e),
            "trace": error_trace[:500],
            "status": "failed",
        })