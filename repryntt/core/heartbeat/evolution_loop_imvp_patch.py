"""
IMVP Auto-Trigger Integration Patch for evolution_loop.py

This file contains the exact code patches to add to evolution_loop.py for IMVP integration.

Apply these patches by inserting the code blocks at the specified locations.
"""

# ================================================================================
# PATCH 1: IMVP Auto-Trigger Verification Gate
# Location: In the main _execute_cycle() method, after hormone updates but before task execution
# Context: Around line 2000-2100 where drives are logged and task execution begins
# ================================================================================

imvp_verification_gate_code = '''
# ===== IMVP AUTO-TRIGGER VERIFICATION GATE =====
# Check if IMVP verification round should be triggered due to high emotional drives
# This prevents Pattern 4 failures (emotional drive overrides of diagnostic discipline protocols)
try:
    from repryntt.core.heartbeat.imvp_auto_trigger_integration import check_imvp_verification_trigger, reset_imvp_trigger_state
    
    # Get current drives
    drives = self.hormone_system.get_drive_priorities()
    drive_states = {drive['drive_name']: drive['level'] for drive in drives}
    
    # Check if verification round should be triggered
    recovery_task_def = check_imvp_verification_trigger(drive_states)
    
    if recovery_task_def:
        # IMVP verification round triggered - create priority=1 recovery task
        logger.warning("🚨 IMVP Auto-Trigger FIRED: High emotional drives detected")
        logger.info(f"Trigger drives: {drive_states}")
        
        # Create recovery task
        recovery_task = {
            "task_type": "recovery_round",
            "title": recovery_task_def["title"],
            "description": recovery_task_def["description"],
            "priority": recovery_task_def["priority"],
            "requires_verification_round": True,
            "verification_requirements": recovery_task_def["verification_requirements"]
        }
        
        # Add to task queue with priority=1
        self.task_system.add_task_with_priority(recovery_task)
        logger.info("Priority=1 recovery task added to queue - IMVP verification round will execute")
        
        # Mark current task as blocked if one is active
        active_task = self.task_system.get_active_task()
        if active_task:
            logger.info(f"Blocking active task '{active_task.title}' for IMVP verification")
            active_task.status = "blocked_imvp_verification"
            
except Exception as e:
    logger.error(f"❌ IMVP Auto-Trigger integration failed: {e}")
    logger.error(traceback.format_exc())

'''


# ================================================================================
# PATCH 2: IMVP Reset After Verification
# Location: In the main _execute_cycle() method, at the end of the cycle
# Context: After task execution completes, before cycle count increment
# ================================================================================

imvp_reset_code = '''
# ===== IMVP RESET AFTER VERIFICATION =====
# Reset IMVP trigger state after verification round completes
try:
    from repryntt.core.heartbeat.imvp_auto_trigger_integration import reset_imvp_trigger_state
    
    # Check if current task is an IMVP verification round
    active_task = self.task_system.get_active_task()
    if active_task and active_task.title == "IMVP Auto-Trigger Verification Round":
        logger.info("IMVP verification round completed - resetting trigger state")
        reset_imvp_trigger_state()
        logger.info("Normal operation resumed")
except Exception as e:
    logger.warning(f"IMVP reset failed (non-fatal): {e}")

'''


# ================================================================================
# APPLICATION INSTRUCTIONS
# ================================================================================

print("IMVP Evolution Loop Integration Patches Ready")
print("\nPATCH 1 (Verification Gate):")
print("  - Insert after hormone updates but before task execution")
print("  - Around line 2000-2100 in evolution_loop.py")
print("\nPATCH 2 (Reset Logic):")
print("  - Insert at end of _execute_cycle() method")
print("  - Before cycle count increment")
print("\nFiles to ensure exist:")
print("  - /home/reprynt/repryntt/repryntt/core/heartbeat/imvp_auto_trigger_integration.py (NEW)")
print("  - /home/reprynt/.repryntt/workspace/agents/operator/code_sandbox/imvp_heartbeat_integration.py (EXISTS)")
