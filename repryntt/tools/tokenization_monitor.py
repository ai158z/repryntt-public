#!/usr/bin/env python3
"""
Detailed Tokenization Monitor - Provides granular breakdown of all tokenized operations
Shows exactly what functionalities are being tokenized and why.
"""

import json
import time
import logging
import threading
from datetime import datetime
from pathlib import Path
import os
import sys

# ai_queue imported lazily in methods to avoid circular import

logger = logging.getLogger(__name__)

class DetailedTokenizationMonitor:
    """Monitors and logs detailed tokenization of all AI operations"""

    def __init__(self):
        # Lazy import to avoid circular import
        from repryntt.routing.ai_queue import master_ai_queue
        self.brain_system = master_ai_queue  # monitoring only needs queue stats
        self.running = False
        self.monitor_thread = None
        self.check_interval = 10  # Check every 10 seconds for detailed logging

        # Detailed tokenization rates with descriptions
        self.detailed_rates = {
            # AI Inference Operations
            'ai_call_text_generation': {
                'cost_per_token': 0.00002,  # 0.002 CR per 100 tokens
                'description': 'Text generation from language model',
                'category': 'ai_inference'
            },
            'ai_call_function_calling': {
                'cost_per_token': 0.000025,  # Slightly higher for tool use
                'description': 'Function calling and tool parameter generation',
                'category': 'ai_inference'
            },

            # Tool Execution Operations
            'tool_brain_network_search': {
                'fixed_cost': 0.005,
                'description': 'Vector similarity search across brain memory',
                'category': 'memory_operations'
            },
            'tool_search_knowledge': {
                'fixed_cost': 0.005,
                'description': 'Search knowledge base and semantic memory',
                'category': 'knowledge_acquisition'
            },
            'tool_grokipedia_search': {
                'fixed_cost': 0.008,
                'description': 'Academic knowledge retrieval and storage',
                'category': 'knowledge_acquisition'
            },
            'tool_google_web_search': {
                'fixed_cost': 0.030,
                'description': 'Web scraping and content extraction',
                'category': 'external_data'
            },
            'tool_web_scrape': {
                'fixed_cost': 0.025,
                'description': 'Extract content from web pages',
                'category': 'external_data'
            },
            'tool_duckduckgo_search': {
                'fixed_cost': 0.020,
                'description': 'Web search using DuckDuckGo',
                'category': 'external_data'
            },
            'tool_create_creative_file': {
                'fixed_cost': 0.003,
                'description': 'Creative content file creation and management',
                'category': 'content_creation'
            },
            'tool_store_knowledge': {
                'fixed_cost': 0.003,
                'description': 'Store new knowledge in semantic memory',
                'category': 'learning'
            },
            'tool_recall_memory': {
                'fixed_cost': 0.001,
                'description': 'Recall information from brain network memory',
                'category': 'learning'
            },
            'tool_brain_network_search': {
                'fixed_cost': 0.002,
                'description': 'Search entire brain network for information',
                'category': 'learning'
            },
            'tool_search_knowledge': {
                'fixed_cost': 0.001,
                'description': 'Search semantic knowledge base',
                'category': 'learning'
            },
            'tool_execute_python': {
                'fixed_cost': 0.010,
                'description': 'Execute Python code in sandbox',
                'category': 'system'
            },
            'tool_file_operations': {
                'fixed_cost': 0.002,
                'description': 'Read/write file operations',
                'category': 'system'
            },

            # Chain of Thought Operations
            'chain_creation': {
                'fixed_cost': 0.010,
                'description': 'Initialize new autonomous reasoning chain',
                'category': 'reasoning'
            },
            'chain_step_execution': {
                'cost_per_token': 0.000015,
                'description': 'Execute individual chain reasoning step',
                'category': 'reasoning'
            },
            'chain_completion': {
                'fixed_cost': 0.050,
                'description': 'Complete full chain of thought with conclusion',
                'category': 'reasoning'
            },

            # Memory Operations
            'memory_semantic_storage': {
                'fixed_cost': 0.002,
                'description': 'Store new semantic knowledge in brain',
                'category': 'learning'
            },
            'memory_episodic_storage': {
                'fixed_cost': 0.001,
                'description': 'Store conversation/interaction memory',
                'category': 'learning'
            },

            # Self-Evolution Operations
            'personality_update': {
                'fixed_cost': 0.020,
                'description': 'Update AI personality traits and behavior',
                'category': 'self_evolution'
            },
            'qlora_training': {
                'fixed_cost': 2.000,
                'description': 'Fine-tune AI model weights for improvement',
                'category': 'self_evolution'
            },

            # System Operations
            'health_check': {
                'fixed_cost': 0.001,
                'description': 'System health monitoring and diagnostics',
                'category': 'system'
            },

            # Consciousness Operations
            'consciousness_meta_decision': {
                'fixed_cost': 0.001,
                'description': 'Meta-decision engine evaluation of consciousness focus',
                'category': 'consciousness'
            },
            'consciousness_attention_allocation': {
                'fixed_cost': 0.006,
                'description': 'Attention allocation across consciousness subsystems',
                'category': 'consciousness'
            },
            'consciousness_goal_operations': {
                'fixed_cost': 0.010,
                'description': 'Goal formation, evolution, and lifecycle management',
                'category': 'consciousness'
            },
            'consciousness_brain_context': {
                'fixed_cost': 0.005,
                'description': 'Query brain system for knowledge and context',
                'category': 'consciousness'
            },
            'consciousness_brain_query': {
                'fixed_cost': 0.005,
                'description': 'Query brain system for knowledge and context',
                'category': 'consciousness'
            },
            'consciousness_subsystem_coordination': {
                'fixed_cost': 0.003,
                'description': 'Coordinate directives across consciousness subsystems',
                'category': 'consciousness'
            },
            'consciousness_cycle_complete': {
                'fixed_cost': 0.034,
                'description': 'Complete consciousness cycle with learning and adaptation',
                'category': 'consciousness'
            }
        }

        # Track recent operations for detailed logging
        self.recent_operations = []
        self.operation_log_file = "logs/detailed_tokenization.jsonl"

        # Ensure log directory exists
        os.makedirs("logs", exist_ok=True)

    def start(self):
        """Start the detailed tokenization monitor"""
        if self.running:
            return

        logger.info("🔍 Starting Detailed Tokenization Monitor...")
        self.running = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

        logger.info("📊 Detailed tokenization logging active - monitoring all AI operations")

    def stop(self):
        """Stop the detailed tokenization monitor"""
        self.running = False
        if self.monitor_thread:
            self.monitor_thread.join(timeout=5)
        logger.info("🛑 Detailed Tokenization Monitor stopped")

    def log_detailed_tokenization(self, operation_type: str, details: dict, cost: float, wallet_balance: float):
        """Log detailed tokenization information"""
        try:
            # Get operation details
            rate_info = self.detailed_rates.get(operation_type, {
                'description': 'Unknown operation',
                'category': 'unknown'
            })

            # Create detailed log entry
            log_entry = {
                'timestamp': datetime.now().isoformat(),
                'operation_type': operation_type,
                'description': rate_info.get('description', 'Unknown'),
                'category': rate_info.get('category', 'unknown'),
                'details': details,
                'cost_cr': round(cost, 6),
                'wallet_balance_after': round(wallet_balance, 4),
                'rate_info': rate_info
            }

            # Write to JSONL file
            with open(self.operation_log_file, 'a') as f:
                f.write(json.dumps(log_entry) + '\n')

            # Console output for visibility
            category_emoji = {
                'ai_inference': '🤖',
                'memory_operations': '🧠',
                'knowledge_acquisition': '📚',
                'external_data': '🌐',
                'content_creation': '🎨',
                'reasoning': '💭',
                'learning': '📖',
                'self_evolution': '🔄',
                'system': '⚙️',
                'consciousness': '🧠',
                'unknown': '❓'
            }

            emoji = category_emoji.get(rate_info.get('category', 'unknown'), '❓')

            logger.info(f"{emoji} TOKENIZED: {operation_type} | {rate_info.get('description', 'Unknown')} | Cost: {cost:.6f} CR | Balance: {wallet_balance:.4f} CR")

            # Store in recent operations for summary
            self.recent_operations.append(log_entry)
            if len(self.recent_operations) > 100:
                self.recent_operations = self.recent_operations[-100:]

        except Exception as e:
            logger.debug(f"❌ Error logging tokenization: {e}")

    def get_tokenization_summary(self, hours: int = 1) -> dict:
        """Get summary of tokenization activity"""
        try:
            cutoff_time = datetime.now().timestamp() - (hours * 3600)

            # Read recent log entries
            if os.path.exists(self.operation_log_file):
                with open(self.operation_log_file, 'r') as f:
                    lines = f.readlines()

                recent_entries = []
                for line in lines[-200:]:  # Check last 200 entries
                    try:
                        entry = json.loads(line.strip())
                        if datetime.fromisoformat(entry['timestamp']).timestamp() > cutoff_time:
                            recent_entries.append(entry)
                    except:
                        continue

                # Calculate summary
                total_cost = sum(entry['cost_cr'] for entry in recent_entries)
                operations_by_category = {}
                operations_by_type = {}

                for entry in recent_entries:
                    category = entry.get('category', 'unknown')
                    op_type = entry.get('operation_type', 'unknown')

                    operations_by_category[category] = operations_by_category.get(category, 0) + 1
                    operations_by_type[op_type] = operations_by_type.get(op_type, 0) + 1

                return {
                    'total_operations': len(recent_entries),
                    'total_cost_cr': round(total_cost, 4),
                    'avg_cost_per_operation': round(total_cost / max(len(recent_entries), 1), 6),
                    'operations_by_category': operations_by_category,
                    'operations_by_type': operations_by_type,
                    'time_period_hours': hours
                }

        except Exception as e:
            logger.debug(f"❌ Error generating summary: {e}")

        return {'error': 'Could not generate summary'}

    def _monitor_loop(self):
        """Main monitoring loop"""
        while self.running:
            try:
                # Check for recent operations and log detailed tokenization
                self._check_recent_operations()

                # Periodic summary logging
                if int(time.time()) % 300 == 0:  # Every 5 minutes
                    summary = self.get_tokenization_summary(hours=1)
                    if 'error' not in summary:
                        logger.info(f"📊 Last hour: {summary['total_operations']} ops | {summary['total_cost_cr']} CR spent | {summary['avg_cost_per_operation']*1000:.3f} mCR/op avg")

                time.sleep(self.check_interval)

            except Exception as e:
                logger.error(f"❌ Error in detailed tokenization monitor: {e}")
                time.sleep(30)

    def _check_recent_operations(self):
        """Check for recent operations that should be logged in detail"""
        # This will be enhanced to hook into the actual operation execution
        # For now, we'll monitor the evolution loop logs for operation indicators
        pass

    def log_manual_operation(self, operation_type: str, details: dict = None):
        """Manually log a tokenization operation for testing"""
        if not details:
            details = {}

        # Estimate cost based on operation type
        rate_info = self.detailed_rates.get(operation_type, {})
        cost = rate_info.get('fixed_cost', 0.001)  # Default small cost

        if 'cost_per_token' in rate_info:
            tokens = details.get('tokens', 100)
            cost = rate_info['cost_per_token'] * tokens

        # Mock balance (in real implementation, get from wallet)
        wallet_balance = 31999.99

        self.log_detailed_tokenization(operation_type, details or {}, cost, wallet_balance)


def start_detailed_tokenization_monitor():
    """Start the detailed tokenization monitor service"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('logs/detailed_tokenization_monitor.log'),
            logging.StreamHandler()
        ]
    )

    monitor = DetailedTokenizationMonitor()
    monitor.start()

    logger.info("🔍 Detailed Tokenization Monitor started")
    logger.info("📊 Monitoring all AI operations with granular detail")
    logger.info("📁 Detailed logs: logs/detailed_tokenization.jsonl")

    try:
        # Keep running and provide periodic summaries
        while True:
            time.sleep(3600)  # Log hourly summary
            summary = monitor.get_tokenization_summary(hours=1)
            if 'error' not in summary:
                logger.info("🕐 Hourly Tokenization Summary:")
                logger.info(f"   Operations: {summary['total_operations']}")
                logger.info(f"   Total Cost: {summary['total_cost_cr']} CR")
                logger.info(f"   By Category: {summary['operations_by_category']}")

    except KeyboardInterrupt:
        logger.info("🛑 Stopping Detailed Tokenization Monitor...")
        monitor.stop()


if __name__ == "__main__":
    start_detailed_tokenization_monitor()