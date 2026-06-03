#!/usr/bin/env python3
"""
Standalone QLoRa Training Runner for SAIGE
Executes QLoRa fine-tuning sessions independently from the evolution loop
"""

import os
import sys
import json
import time
import logging
from datetime import datetime, timedelta
import pytz
from pathlib import Path
from repryntt.paths import data_dir

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(str(Path(__file__).resolve().parent.parent.parent / 'logs' / 'qlora_training.log')),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class QLoRaTrainingRunner:
    """Standalone runner for QLoRa training sessions"""

    def __init__(self):
        self.training_window_start = 0  # 12 AM EST
        self.training_window_end = 2    # 2 AM EST
        self.timezone = pytz.timezone('US/Eastern')

    def is_training_window(self):
        """Check if current time is within training window (3 AM - 5 AM EST).

        Restricted to low-activity hours to avoid CUDA OOM on Jetson
        (shared 8GB RAM with camera/vision pipeline).
        """
        try:
            import pytz
            from datetime import datetime
            hour = datetime.now(pytz.timezone('US/Eastern')).hour
            return 3 <= hour < 5
        except Exception:
            return False  # If timezone fails, don't train

    def has_sufficient_data(self, min_examples=10):
        """Check if there's sufficient training data"""
        training_data_path = str(data_dir() / "training_data.json")

        if not os.path.exists(training_data_path):
            return False

        try:
            with open(training_data_path, 'r') as f:
                data = json.load(f)
                return len(data) >= min_examples
        except Exception as e:
            logger.error(f"Error checking training data: {e}")
            return False

    def run_training_session(self):
        """Execute a QLoRa training session"""
        try:
            logger.info("🔥 Starting standalone QLoRa training session")

            # Check if we're in training window
            if not self.is_training_window():
                logger.info("⏰ Not in training window (12 AM - 2 AM EST) - skipping training")
                return False

            # Check if we have sufficient data
            if not self.has_sufficient_data():
                logger.info("📊 Insufficient training data - need at least 10 examples")
                return False

            # Import and run QLoRa trainer
            from qlora_trainer import QLoRaTrainer
            trainer = QLoRaTrainer()
            trainer.run_training_session()

            logger.info("✅ QLoRa training session completed successfully")
            return True

        except Exception as e:
            logger.error(f"❌ QLoRa training session failed: {e}")
            return False

    def run_continuous_training(self, check_interval=3600):
        """Run continuous training that checks periodically"""
        logger.info("🚀 Starting continuous QLoRa training monitor")
        logger.info(f"Training window: {self.training_window_start}:00 - {self.training_window_end}:00 EST")
        logger.info(f"Check interval: {check_interval} seconds")

        while True:
            try:
                # Check if it's time to run training
                if self.is_training_window() and self.has_sufficient_data():
                    logger.info("📅 Training window active and data available - starting training")
                    success = self.run_training_session()

                    if success:
                        # Wait longer after successful training
                        logger.info("⏸️ Training completed - sleeping for 24 hours")
                        time.sleep(24 * 3600)  # 24 hours
                    else:
                        # Retry sooner if failed
                        logger.info("⏸️ Training failed - retrying in 1 hour")
                        time.sleep(3600)  # 1 hour
                else:
                    # Log status periodically
                    current_time = datetime.now(self.timezone)
                    next_check = current_time + timedelta(seconds=check_interval)
                    logger.info(f"⏸️ Waiting for training window or more data. Next check: {next_check.strftime('%H:%M %Z')}")
                    time.sleep(check_interval)

            except KeyboardInterrupt:
                logger.info("🛑 Continuous training stopped by user")
                break
            except Exception as e:
                logger.error(f"❌ Error in continuous training loop: {e}")
                time.sleep(300)  # Wait 5 minutes before retrying

def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='Standalone QLoRa Training Runner')
    parser.add_argument('--continuous', action='store_true',
                       help='Run continuous training monitor')
    parser.add_argument('--force', action='store_true',
                       help='Force training even outside window')
    parser.add_argument('--check-interval', type=int, default=3600,
                       help='Check interval in seconds for continuous mode (default: 3600)')

    args = parser.parse_args()

    runner = QLoRaTrainingRunner()

    if args.force:
        logger.info("⚡ FORCE MODE: Ignoring training window restrictions")
        runner.training_window_start = 0
        runner.training_window_end = 24  # All day

    if args.continuous:
        logger.info("🔄 Starting continuous training mode")
        runner.run_continuous_training(args.check_interval)
    else:
        logger.info("🎯 Running single training session")
        success = runner.run_training_session()
        return 0 if success else 1

if __name__ == "__main__":
    exit(main())
