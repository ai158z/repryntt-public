#!/usr/bin/env python3
"""
SAIGE Economy Monitor - Separate service for tokenizing successful AI operations
This runs independently of the core evolution loop to avoid resource conflicts.
"""

import json
import time
import logging
import threading
from datetime import datetime
from pathlib import Path
import os
import sys

# Add brain system for monitoring
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from brain.brain_system import BrainSystem

logger = logging.getLogger(__name__)

class SAIGEEconomyMonitor:
    """Monitors successful AI operations and tokenizes them"""

    def __init__(self):
        # IMPORTANT: This script is legacy and can conflict with the core brain process
        # if both try to start the blockchain. Make sure the core process is the one
        # starting the economy (recommended) by using:
        #   SAIGE_ENABLE_ECONOMY=1 ./start_saige_production.sh --with-economy
        #
        # If you run this monitor, it will NOT force-enable the economy; it will only
        # initialize/start it if the environment already enabled it.
        self.brain_system = BrainSystem()
        self.running = False
        self.monitor_thread = None
        self.check_interval = 60  # Check every 60 seconds
        self.last_processed_timestamp = time.time()

        # Tokenization rates (credits per operation)
        self.tokenization_rates = {
            'successful_ai_call': 0.001,      # Basic AI inference
            'tool_execution': 0.005,          # Tool usage
            'chain_completion': 0.05,         # Completed chain of thought
            'memory_storage': 0.002,          # Learning/memory storage
            'qlora_training': 2.0,            # Model improvement
            'evolution_cycle': 0.01           # Full evolution cycle
        }

    def start(self):
        """Start the economy monitor"""
        if self.running:
            return

        logger.info("💰 Starting SAIGE Economy Monitor...")
        self.running = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

        # Initialize economy if available
        self._initialize_economy()

        logger.info("✅ Economy Monitor started - will tokenize successful AI operations")

    def stop(self):
        """Stop the economy monitor"""
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        logger.info("🛑 Economy Monitor stopped")

    def _initialize_economy(self):
        """Initialize the robot economy system"""
        try:
            if not hasattr(self.brain_system, 'robot_economy_manager') or not self.brain_system.robot_economy_manager:
                logger.warning("🤖 Robot Economy system not available")
                return

            logger.info("🤖 Initializing Robot Economy for tokenization...")
            result = self.brain_system.robot_economy_manager.start_economy()

            if result.get("success", False):
                logger.info(f"✅ Robot Economy started: {result.get('nodes', 0)} nodes, "
                          f"{result.get('miners', 0)} miners, {result.get('ais', 0)} AIs")
            else:
                logger.error(f"❌ Failed to start Robot Economy: {result.get('error', 'Unknown error')}")

        except Exception as e:
            logger.error(f"❌ Error initializing economy: {e}")

    def _monitor_loop(self):
        """Main monitoring loop"""
        while self.running:
            try:
                self._check_for_new_operations()
                time.sleep(self.check_interval)
            except Exception as e:
                logger.error(f"❌ Error in economy monitor loop: {e}")
                time.sleep(30)

    def _check_for_new_operations(self):
        """Check for new successful AI operations to tokenize"""
        try:
            # Check evolution loop log for successful operations
            log_file = Path("logs/evolution_loop.log")
            if not log_file.exists():
                return

            # Read recent log entries
            with open(log_file, 'r') as f:
                lines = f.readlines()[-100:]  # Last 100 lines

            new_operations = []
            current_time = time.time()

            for line in lines:
                if "✅ COMPLETED:" in line and current_time - self.last_processed_timestamp < 3600:  # Last hour
                    # Parse successful operation
                    operation = self._parse_successful_operation(line)
                    if operation:
                        new_operations.append(operation)

            # Tokenize successful operations
            for operation in new_operations:
                self._tokenize_operation(operation)

            if new_operations:
                self.last_processed_timestamp = current_time
                logger.info(f"💰 Tokenized {len(new_operations)} successful AI operations")

        except Exception as e:
            logger.error(f"❌ Error checking operations: {e}")

    def _parse_successful_operation(self, log_line: str) -> dict:
        """Parse a successful operation from log line"""
        try:
            # Extract operation type and details
            if "COMPLETED:" in log_line:
                # Format: "✅ COMPLETED: request_id (time, tokens)"
                parts = log_line.split("COMPLETED:")
                if len(parts) > 1:
                    details = parts[1].strip()
                    return {
                        'type': 'successful_ai_call',
                        'details': details,
                        'timestamp': time.time()
                    }
        except Exception as e:
            logger.debug(f"Could not parse operation from line: {log_line[:100]}")

        return None

    def _tokenize_operation(self, operation: dict):
        """Tokenize a successful operation"""
        try:
            if not hasattr(self.brain_system, 'robot_economy_manager') or not self.brain_system.robot_economy_manager:
                return

            operation_type = operation.get('type', 'successful_ai_call')
            credits_amount = self.tokenization_rates.get(operation_type, 0.001)

            # Get AI wallet address (don't create new wallet each time!)
            from brain.brain_system import get_ai_wallet_address
            ai_wallet_address = get_ai_wallet_address(self.brain_system)
            
            # Reward using proper reward transaction, not faucet
            reward_result = self.brain_system.robot_economy_manager.reward_ai_for_task(
                ai_wallet_address, 
                credits_amount,
                operation_type
            )

            if reward_result.get('success'):
                logger.info(f"💰 Rewarded {credits_amount:.4f} CR for {operation_type}")
            else:
                logger.debug(f"⚠️ Failed to tokenize {operation_type}: {reward_result.get('error')}")

        except Exception as e:
            logger.debug(f"❌ Error tokenizing operation: {e}")

    def monitor_economy_health(self):
        """Monitor overall economy health"""
        try:
            if not hasattr(self.brain_system, 'robot_economy_manager') or not self.brain_system.robot_economy_manager:
                return

            # Get economy status
            monitoring_data = self.brain_system.monitor_robot_economy()

            if monitoring_data.get("success", False):
                status = monitoring_data.get("economy_status", {})
                analysis = monitoring_data.get("analysis", {})
                metrics = status.get("metrics", {})

                health = analysis.get('overall_health', 'unknown')
                logger.info(f"🤖 Economy Health: {health.upper()} | "
                          f"Miners: {metrics.get('active_miners', 0)} | "
                          f"Blocks: {metrics.get('total_blocks', 0)}")

                # Store economy state in brain
                economy_state = {
                    "health": health,
                    "metrics": metrics,
                    "timestamp": monitoring_data.get("timestamp")
                }

                self.brain_system.store_semantic_memory(
                    topic="robot_economy_monitoring",
                    content=f"Economy monitoring: {json.dumps(economy_state)}",
                    domain="economics",
                    confidence=0.8
                )

        except Exception as e:
            logger.debug(f"❌ Error monitoring economy health: {e}")


def start_economy_monitor():
    """Start the economy monitor service"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('logs/economy_monitor.log'),
            logging.StreamHandler()
        ]
    )

    monitor = SAIGEEconomyMonitor()
    monitor.start()

    logger.info("💰 SAIGE Economy Monitor started - tokenizing successful AI operations")
    logger.info("📊 Monitor interval: 60 seconds")
    logger.info("🎯 Will reward successful AI calls, tool usage, and learning activities")

    try:
        # Keep running and periodically check economy health
        while True:
            time.sleep(300)  # Check every 5 minutes
            monitor.monitor_economy_health()

    except KeyboardInterrupt:
        logger.info("🛑 Stopping Economy Monitor...")
        monitor.stop()


if __name__ == "__main__":
    start_economy_monitor()