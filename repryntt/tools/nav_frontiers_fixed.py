"""
Fixed nav_frontiers tool with numpy type conversion for JSON serialization.
This is a drop-in replacement for the broken nav_frontiers in registry.py.
"""

import json
import traceback


def nav_frontiers_fixed(max_count: int = 5, **kw) -> str:
    """Fixed version of nav_frontiers with numpy type conversion.

    List the nearest unexplored frontier cells from the
    occupancy grid — edges of known territory.

    Each frontier has world coords (cm), distance (m), and
    a bearing relative to the robot's current heading.
    Use these as concrete navigation goals instead of
    abstract terms like 'the hallway'.
    """
    try:
        from repryntt.hardware.spatial_map import get_spatial_map
        from repryntt.hardware.local_perception import (
            OccupancyGrid,
        )
        from repryntt.hardware.spatial_context import (
            _bearing_to, _bearing_phrase,
        )
        import numpy as np

        def convert_types_to_native(data):
            """Recursively convert numpy types and other non-JSON-serializable types to native Python types.

            Handles: np.int64, np.int32, np.float64, np.float32, np.int8, etc.
            Also handles numpy bool, bytes, and any numpy generic type.
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

        smap = get_spatial_map()
        grid = OccupancyGrid()
        max_count = max(1, min(20, int(max_count)))
        cells = grid.get_frontier_cells(
            float(smap.x), float(smap.y),
            max_count=max_count)
        items = []
        for c in cells:
            if len(c) < 3:
                continue
            fx, fy = c[0], c[1]
            dist_cm, delta = _bearing_to(
                float(smap.x), float(smap.y),
                float(smap.heading), fx, fy)
            # Convert numpy types to native Python before JSON serialization
            items.append({
                "x_cm": round(convert_types_to_native(fx), 1),
                "y_cm": round(convert_types_to_native(fy), 1),
                "distance_m": round(convert_types_to_native(dist_cm) / 100.0, 2),
                "bearing_deg": round(convert_types_to_native(delta), 1),
                "direction": _bearing_phrase(delta),
            })
        return json.dumps({
            "robot_pose": {
                "x_cm": round(float(smap.x), 1),
                "y_cm": round(float(smap.y), 1),
                "heading_deg": round(float(smap.heading), 1),
            },
            "frontier_count": len(items),
            "frontiers": items,
        })
    except Exception as e:
        return json.dumps({"error": f"frontiers unavailable: {e}", "traceback": traceback.format_exc()})
