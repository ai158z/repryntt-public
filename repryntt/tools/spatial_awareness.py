"""
repryntt.tools.spatial_awareness — Spatial navigation and mapping tools registry.

This module registers all spatial awareness tools including the fixed nav_frontiers.
"""

import json
import logging

logger = logging.getLogger(__name__)


def register_spatial_awareness_tools(registry) -> int:
    """Register all spatial awareness tools with the registry.
    
    Returns:
        int: Number of tools registered
    """
    count = 0
    
    # Import the fixed nav_frontiers implementation
    try:
        from .nav_frontiers import nav_frontiers, convert_types_to_native
        from .nav_frontiers_fixed_registry import nav_frontiers_fixed
        
        # Register nav_frontiers with the fixed implementation
        registry.register(
            "nav_frontiers", 
            nav_frontiers,
            category="spatial_awareness"
        )
        count += 1
        logger.info("✅ nav_frontiers registered with fixed JSON serialization")
        
        # Register the type conversion utility function
        registry.register(
            "convert_types_to_native",
            convert_types_to_native,
            category="spatial_awareness"
        )
        count += 1
        logger.info("✅ convert_types_to_native utility registered")
        
        # Register the alternative fixed implementation as an alias
        registry.register(
            "nav_frontiers_fixed",
            nav_frontiers_fixed,
            category="spatial_awareness"
        )
        count += 1
        logger.info("✅ nav_frontiers_fixed registered as alternative")
        
    except Exception as e:
        logger.error(f"⚠️ Failed to register spatial awareness tools: {e}")
        logger.error(f"Stack trace: {__import__('traceback').format_exc()}")
    
    return count
