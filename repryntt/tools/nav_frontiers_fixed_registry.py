"""
repryntt.tools.nav_frontiers_fixed_registry — Fixed tool registration for nav_frontiers.

This file is a drop-in replacement for the nav_frontiers tool registration
to ensure the JSON serialization bug is fixed at the tool level.
"""

import json
import logging
import traceback

import numpy as np

from repryntt.hardware.spatial_map import get_spatial_map
from repryntt.hardware.local_perception import OccupancyGrid

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

        logger.debug(f"get_frontier_cells returned {len(cells_raw)} cells")
        if cells_raw:
            logger.debug(f"First cell types: {[type(c).__name__ for c in cells_raw[0]]}")
            logger.debug(f"First cell values: {cells_raw[0]}")

        # Convert numpy types to native Python before building response
        items = []
        for c in cells_raw:
            # Each cell is a tuple: (world_x, world_y, distance, bearing)
            if len(c) >= 3:
                world_x, world_y, distance_m = c[0], c[1], c[2]
                bearing_deg = c[3] if len(c) > 3 else 0.0

                frontier = {
                    'world_x_cm': convert_types_to_native(world_x),
                    'world_y_cm': convert_types_to_native(world_y),
                    'distance_m': convert_types_to_native(distance_m),
                    'bearing_deg': convert_types_to_native(bearing_deg)
                }
                items.append(frontier)

        response = {
            'robot_pose': {
                'x_cm': convert_types_to_native(smap.x),
                'y_cm': convert_types_to_native(smap.y),
                'heading_deg': convert_types_to_native(smap.heading),
            },
            'frontiers': items,
            'count': len(items),
            'max_count_requested': max_count,
            'source': 'monocular (Depth Anything v2)'
        }

        # Test JSON serialization before returning
        json_result = json.dumps(response, indent=2)
        logger.debug(f"JSON serialization successful. Length: {len(json_result)} chars")
        return json_result

    except Exception as e:
        logger.error(f"nav_frontiers failed: {e}")
        logger.error(traceback.format_exc())
        error_response = {
            'error': str(e),
            'error_type': type(e).__name__,
            'traceback': traceback.format_exc()
        }
        logger.error(f"Error response: {error_response}")
        return json.dumps(error_response)