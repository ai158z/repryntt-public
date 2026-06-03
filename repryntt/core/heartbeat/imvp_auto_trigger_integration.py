"""
IMVP Auto-Trigger Integration Module for evolution_loop.py

This module integrates the Inner Monologue Verification Protocol (IMVP) auto-trigger
system into the SAIGE evolution loop to prevent Pattern 4 failures where emotional drives
override diagnostic discipline protocols.

Integration point: evolution_loop.py main cycle loop
- Monitors GUARDIAN and EVOLUTION drives
- Automatically triggers verification rounds with priority=1 when drives exceed 0.70
- Resets trigger state after verification completion
- Non-invasive to core agent infrastructure
"""

import logging
import time
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Import IMVPAutoTrigger from code sandbox (already validated)
try:
    from code_sandbox.imvp_heartbeat_integration import IMVPAutoTrigger, integrate_imvp_into_heartbeat
    logger.info("✅ IMVP Auto-Trigger integration module loaded successfully")
except ImportError as e:
    logger.warning(f"⚠️ IMVP Auto-Trigger module not found: {e}")
    logger.warning("⚠️ IMVP verification will not auto-trigger during high-emotion cycles")
    IMVPAutoTrigger = None
    integrate_imvp_into_heartbeat = None


def check_imvp_verification_trigger(drives: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """
    Check if IMVP verification round should be triggered based on drive states.
    
    Args:
    drives: Dictionary containing drive states including GUARDIAN and EVOLUTION
    
    Returns:
    Optional[Dict]: Recovery task definition if verification round should be triggered, None otherwise
    """
    
    if IMVPAutoTrigger is None:
        logger.warning("IMVP Auto-Trigger system not available - skipping verification check")
        return None

    # Initialize IMVP auto-trigger system
    imvp_trigger = IMVPAutoTrigger()

    # Check if verification round should be triggered
    result = imvp_trigger.should_trigger_verification(drives)
    if result:
        return create_imvp_recovery_task()
    return None


def create_imvp_recovery_task() -> Dict[str, Any]:
    """
    Create recovery task definition for IMVP auto-trigger verification round.
    
    Returns:
    Dict: Task definition with priority=1 for verification round
    """
    return {
        "task_type": "recovery_round",
        "title": "IMVP Auto-Trigger Verification Round",
        "description": (
            "Automated verification round triggered due to high emotional drives "
            "(GUARDIAN > 0.70 OR EVOLUTION > 0.70). "
            "This prevents Pattern 4 failures where emotional overrides skip diagnostic discipline protocols. "
            "Runs TRV/DCC/QGD verification with Creativity measurement before allowing any task to complete."
        ),
        "priority": 1,
        "requires_verification_round": True,
        "verification_requirements": [
            "TRV (Tool Result Verification)",
            "DCC (Data Consistency Check)",
            "BCE (Bootstrap Coherence Enforcement)",
            "QGD (Quality Gate for Deliverables)",
            "IMVP (Inner Monologue Verification Protocol)"
        ]
    }


def reset_imvp_trigger_state():
    """
    Reset IMVP trigger state after verification round completes.
    """
    if IMVPAutoTrigger is not None:
        IMVPAutoTrigger.reset_trigger_state()
        logger.info("IMVP Auto-Trigger state reset - normal operation resumed")


def log_imvp_verification_event(event_type: str, details: Dict[str, Any] = None):
    """
    Log IMVP verification events for monitoring and debugging.
    
    Args:
    event_type: Type of event (triggered, completed, skipped, error)
    details: Additional event details
    """
    event = {
        'timestamp': time.time(),
        'event_type': event_type,
        'details': details or {}
    }
    
    if event_type == 'triggered':
        logger.warning(f"🚨 IMVP Auto-Trigger FIRED: {details}")
    elif event_type == 'completed':
        logger.info(f"✅ IMVP Auto-Trigger COMPLETED: {details}")
    elif event_type == 'skipped':
        logger.info(f"⏭️ IMVP Auto-Trigger SKIPPED: {details}")
    elif event_type == 'error':
        logger.error(f"❌ IMVP Auto-Trigger ERROR: {details}")


# Legacy compatibility - will be removed in future versions
imvp_auto_trigger_check = check_imvp_verification_trigger
imvp_auto_trigger_task = create_imvp_recovery_task
imvp_auto_trigger_reset = reset_imvp_trigger_state
imvp_auto_trigger_log = log_imvp_verification_event
"""
IMVP Auto-Trigger Integration Module for evolution_loop.py

This module integrates the Inner Monologue Verification Protocol (IMVP) auto-trigger
system into the SAIGE evolution loop to prevent Pattern 4 failures where emotional drives
override diagnostic discipline protocols.

Integration point: evolution_loop.py main cycle loop
- Monitors GUARDIAN and EVOLUTION drives
- Automatically triggers verification rounds with priority=1 when drives exceed 0.70
- Resets trigger state after verification completion
- Non-invasive to core agent infrastructure
"""

import logging
import time
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Import IMVPAutoTrigger from code sandbox (already validated)
try:
    from code_sandbox.imvp_heartbeat_integration import IMVPAutoTrigger, integrate_imvp_into_heartbeat
    logger.info("✅ IMVP Auto-Trigger integration module loaded successfully")
except ImportError as e:
    logger.warning(f"⚠️ IMVP Auto-Trigger module not found: {e}")
    logger.warning("⚠️ IMVP verification will not auto-trigger during high-emotion cycles")
    IMVPAutoTrigger = None
    integrate_imvp_into_heartbeat = None


def check_imvp_verification_trigger(drives: Dict[str, float]) -> Optional[Dict[str, Any]]:
    """
    Check if IMVP verification round should be triggered based on drive states.
    
    Args:
    drives: Dictionary containing drive states including GUARDIAN and EVOLUTION
    
    Returns:
    Optional[Dict]: Recovery task definition if verification round should be triggered, None otherwise
    """
    
    if IMVPAutoTrigger is None:
        logger.warning("IMVP Auto-Trigger system not available - skipping verification check")
        return None

    # Initialize IMVP auto-trigger system
    imvp_trigger = IMVPAutoTrigger()

    # Check if verification round should be triggered
    result = imvp_trigger.should_trigger_verification(drives)
    if result:
        return create_imvp_recovery_task()
    return None


def create_imvp_recovery_task() -> Dict[str, Any]:
    """
    Create recovery task definition for IMVP auto-trigger verification round.
    
    Returns:
    Dict: Task definition with priority=1 for verification round
    """
    return {
        "task_type": "recovery_round",
        "title": "IMVP Auto-Trigger Verification Round",
        "description": (
            "Automated verification round triggered due to high emotional drives "
            "(GUARDIAN > 0.70 OR EVOLUTION > 0.70). "
            "This prevents Pattern 4 failures where emotional overrides skip diagnostic discipline protocols. "
            "Runs TRV/DCC/QGD verification with Creativity measurement before allowing any task to complete."
        ),
        "priority": 1,
        "requires_verification_round": True,
        "verification_requirements": [
            "TRV (Tool Result Verification)",
            "DCC (Data Consistency Check)",
            "BCE (Bootstrap Coherence Enforcement)",
            "QGD (Quality Gate for Deliverables)",
            "IMVP (Inner Monologue Verification Protocol)"
        ]
    }


def reset_imvp_trigger_state():
    """
    Reset IMVP trigger state after verification round completes.
    """
    if IMVPAutoTrigger is not None:
        IMVPAutoTrigger.reset_trigger_state()
        logger.info("IMVP Auto-Trigger state reset - normal operation resumed")


def log_imvp_verification_event(event_type: str, details: Dict[str, Any] = None):
    """
    Log IMVP verification events for monitoring and debugging.
    
    Args:
    event_type: Type of event (triggered, completed, skipped, error)
    details: Additional event details
    """
    event = {
        'timestamp': time.time(),
        'event_type': event_type,
        'details': details or {}
    }
    
    if event_type == 'triggered':
        logger.warning(f"🚨 IMVP Auto-Trigger FIRED: {details}")
    elif event_type == 'completed':
        logger.info(f"✅ IMVP Auto-Trigger COMPLETED: {details}")
    elif event_type == 'skipped':
        logger.info(f"⏭️ IMVP Auto-Trigger SKIPPED: {details}")
    elif event_type == 'error':
        logger.error(f"❌ IMVP Auto-Trigger ERROR: {details}")


# Legacy compatibility - will be removed in future versions
imvp_auto_trigger_check = check_imvp_verification_trigger
imvp_auto_trigger_task = create_imvp_recovery_task
imvp_auto_trigger_reset = reset_imvp_trigger_state
imvp_auto_trigger_log = log_imvp_verification_event
