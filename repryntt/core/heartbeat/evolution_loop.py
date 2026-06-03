#!/usr/bin/env python3
"""
SAIGE Autonomous Evolution Loop - Python Implementation
Hormone-driven self-evolution with QLoRa fine-tuning
"""

import json
import time
import traceback
import logging
import subprocess
import signal
import os
import re
import threading
import queue
from datetime import datetime
from typing import Dict, List, Any, Optional
from pathlib import Path
import sys

# Fix Windows cp1252 console encoding before any log output
from repryntt.platform_utils import fix_windows_encoding
fix_windows_encoding()

import pytz
import requests

# Global AI Request Queue - ensures only one AI request at a time
# REMOVED: Duplicate AIRequestQueue class
# All AI requests now go through the global master_ai_queue from brain_system.py
# This prevents the dual-queue architecture that was causing timeouts

# Consciousness daemon
from repryntt.core.consciousness import start_consciousness_daemon, report_directive_activity, get_consciousness_status

# Import infinite self-prompting chains
from repryntt.tools.self_prompting import initialize_self_prompting_chains

# Add brain system for unified memory management
from repryntt.brain import get_brain_system, create_brain_system, BrainSystemProtocol
from repryntt.routing.ai_queue import master_ai_queue
from repryntt.paths import data_dir, models_dir, get_data_dir

# Centralized output processor (chain completion signals, goals, directives)
from repryntt.tools.output_processor import AIOutputProcessor

# Task system — actionable task queue replaces chain-first exploration
from repryntt.agents.task_system import TaskSystem

# Micro-chain engine — sequential reasoning for small local LLMs (proven on 3B models)
from repryntt.core.micro_chain_engine import execute_task as micro_chain_execute, classify_task_type

# Evolution bootstrap — persistent identity, priorities, memory brief (like Jarvis bootstrap)
from repryntt.core.evolution.bootstrap_manager import EvolutionBootstrapManager

# Setup logging
_log_dir = Path.home() / ".repryntt" / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(str(_log_dir / 'saige_evolution.log')),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

class SAIGEEvolutionLoop:
    """Main evolution loop for SAIGE autonomous learning"""

    def __init__(self):
        # Configuration
        self.cycle_interval = 0  # Continuous processing without sleep cycles
        self.update_freq = 1  # Legacy — actual interval is 69 min (see micro_lora_trainer_production.py)
        self.max_cycles = None  # Run indefinitely
        self.qlora_enabled = False  # QLoRa self-evolution DISABLED
        
        # Verify global master queue is available
        logger.info(f"🎯 Initializing with GLOBAL MasterAIQueue instance: {id(master_ai_queue)}")

        # Initialize unified brain system (vector search handled internally)
        self.brain_system = get_brain_system()
        self.brain_file = "node2040_brain.json"  # Keep for compatibility

        # Connect Jarvis bridge so gem hunter / Andrew can use invoke_jarvis
        try:
            from repryntt.agents.persistent_agents import get_agent_daemon
            daemon = get_agent_daemon(auto_start=False)
            self.brain_system._daemon_ref = daemon
            logger.info("🌉 Jarvis bridge connected to evolution loop")
        except Exception as e:
            logger.warning(f"🌉 Jarvis bridge not available (gem hunter will be limited): {e}")

        # Initialize infinite self-prompting chains for continuous exploration
        # TEMPORARILY DISABLED - causing errors, need to debug
        # self.self_prompting_chains = initialize_self_prompting_chains(self.brain_system, self)
        # self.self_prompting_chains.start_background_exploration()
        self.self_prompting_chains = None

        # Restore rich personality structure for self-evolution
        self._restore_rich_personality_structure()
        self.running = True

        # ===== CONTEXT COMPACTION (Tier 1 — 4K context window) =====
        try:
            from repryntt.core.memory.context_compaction import ContextCompactor
            self.context_compactor = ContextCompactor(
                context_window=4096,
                llm_endpoint="http://localhost:8080",
                reserve_for_response=512,
                reserve_for_system=512,
            )
            logger.info("📦 Context compaction initialized (4K window, OpenClaw-style)")
        except ImportError as e:
            logger.warning(f"Context compaction not available: {e}")
            self.context_compactor = None

        # ===== SKILL LOADER (OpenClaw-style modular intelligence) =====
        from repryntt.core.heartbeat.skill_loader import EvolutionSkillLoader
        self.skill_loader = EvolutionSkillLoader()
        self.skill_loader.scan(force=True)
        logger.info("📚 Skill loader initialized for evolution loop")

        # ===== ALGORITHMIC HORMONE SYSTEM =====
        # Neuroscience-based: Schultz RPE, Lövheim's Cube, Homeostatic Control,
        # Cañamero Deficit Motivation, Solomon-Corbit Opponent Process, Panksepp Circuits
        from repryntt.core.hormones.algorithmic_hormone_system import AlgorithmicHormoneSystem
        self.hormone_system = AlgorithmicHormoneSystem(brain_path="brain/ava_brain.json")

        # Bridge hormone system to brain_system so conversation layer can fire/read events
        self.brain_system.set_hormone_system(self.hormone_system)

        # Backward-compatible hormones dict (updated from hormone_system each cycle)
        self.hormones = dict(self.hormone_system.levels)

        # Evolution state
        self.cycle_count = 0
        self.training_data = []

        # Activity tracking for brain monitor
        self.activities_log = []
        self.workloads_log = []
        self.max_log_entries = 50

        # Data feeder status tracking - will be updated from feeder coordinator data
        self.feeder_status = {
            'news_feeder': {'status': 'inactive', 'stimulus': 0.0},
            'web_search_feeder': {'status': 'inactive', 'stimulus': 0.0},
            'sensor_feeder': {'status': 'inactive', 'stimulus': 0.0},
            'conversation_feeder': {'status': 'inactive', 'stimulus': 0.0},
            'curiosity_feeder': {'status': 'inactive', 'stimulus': 0.0}
        }

        # Path to aggregated stimulus file from feeder coordinator
        self.aggregated_stimulus_file = 'data/aggregated_stimulus.json'

        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # Initialize continuous consciousness daemon with shared brain system
        from repryntt.core.consciousness.daemon import SAIGEConsciousnessDaemon
        self.consciousness_daemon = SAIGEConsciousnessDaemon(brain_system=self.brain_system)
        self.consciousness_daemon.start()

        # WorldState runtime integration disabled. The evolution loop keeps its
        # existing consciousness/hormone systems without connecting them to the
        # removed unified awareness layer.
        self._world_state = None

        # Initialize external API service (economy temporarily disabled)
        from repryntt.web.external_api import start_external_api_service
        self.external_api_service = start_external_api_service(
            brain_system=self.brain_system,
            robot_economy_manager=None  # Temporarily disabled
        )

        # Initialize master AI queue (but we use direct calls now)
        # self.brain_system.start_master_queue()

        # Initialize task system for actionable work
        self.task_system = TaskSystem()
        logger.info(f"📋 Task system initialized: {self.task_system.queue_size()} queued tasks")
        
        # Evolution bootstrap manager — persistent identity, priorities, memory brief
        # Gives the local LLM the same persistent cognition that Jarvis has via bootstrap files
        self.bootstrap_mgr = EvolutionBootstrapManager(
            brain_system=self.brain_system,
            task_system=self.task_system,
            hormone_system=self.hormone_system,
        )
        logger.info("📋 Evolution bootstrap manager initialized")

        # ===== LLM ORCHESTRATION LEARNER =====
        # Learns to use the local model smarter: context budget optimization,
        # task-type escalation, and output quality gating.
        # Scales with model capability — tiny models get routing help,
        # big models get learning data injected into prompts.
        try:
            from repryntt.learning.llm_learner import get_llm_learner
            self.llm_learner = get_llm_learner()
            # Detect model capabilities from config
            from repryntt.routing.provider_router import load_ai_provider_config
            ai_config = load_ai_provider_config(Path("config"))
            provider = ai_config.get("provider", "local")
            settings = ai_config.get(provider, ai_config.get("local", {}))
            ctx_window = settings.get("context_window", 4096)
            model_name = settings.get("model", "default")
            self.llm_learner.detect_model_capabilities(ctx_window, model_name)
            logger.info(f"🧪 LLM learner initialized (model={model_name}, ctx={ctx_window})")
        except Exception as e:
            logger.warning(f"⚠️ LLM learner init failed (non-fatal): {e}")
            self.llm_learner = None

        # Brain system already initialized in __init__
        logger.info("🧠 SAIGE Evolution Loop initialized with continuous consciousness and unified brain system")
        
        # Run morning startup self-prompt (now generates tasks)
        self._run_morning_startup()

        # Generate initial memory brief so we have context from cycle 1
        try:
            self.bootstrap_mgr.update_memory_brief()
        except Exception as e:
            logger.warning(f"Initial memory brief generation failed (non-fatal): {e}")

    def log_activity(self, activity_type: str, description: str, details: Dict[str, Any] = None):
        """Log an activity for brain monitoring"""
        activity = {
            'timestamp': time.time(),
            'type': activity_type,
            'description': description,
            'details': details or {},
            'cycle': self.cycle_count
        }
        self.activities_log.append(activity)
        if len(self.activities_log) > self.max_log_entries:
            self.activities_log = self.activities_log[-self.max_log_entries:]

    def log_workload(self, workload_type: str, description: str, status: str = 'started', duration: float = None, result: str = None):
        """Log a workload for brain monitoring"""
        workload = {
            'timestamp': time.time(),
            'type': workload_type,
            'description': description,
            'status': status,
            'duration': duration,
            'result': result,
            'cycle': self.cycle_count
        }
        self.workloads_log.append(workload)
        if len(self.workloads_log) > self.max_log_entries:
            self.workloads_log = self.workloads_log[-self.max_log_entries:]

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.running = False

    def _run_morning_startup(self):
        """Execute morning startup self-prompt for SAIGE to plan its day"""
        try:
            from repryntt.core.heartbeat.morning_startup_prompt import execute_morning_startup
            logger.info("🌅 Running morning startup self-prompt...")
            
            result = execute_morning_startup(self.brain_system)
            
            if result.get('success'):
                logger.info("✅ Morning startup complete - SAIGE has planned its day")
                # Log the activity
                self.log_activity(
                    'morning_startup',
                    'Daily goal planning and self-reflection',
                    {'daily_plan_length': len(result.get('daily_plan', ''))}
                )
                # Hormone event: morning startup
                self.hormone_system.process_event('morning_startup', {'magnitude': 1.0})
            else:
                logger.warning(f"⚠️ Morning startup failed: {result.get('error')}")
                
        except Exception as e:
            logger.error(f"❌ Morning startup error: {e}")
            import traceback
            traceback.print_exc()

    def check_ai_server(self) -> bool:
        """Check if the AI server is available and responding"""
        try:
            import requests
            logger.debug("🔍 Checking AI server availability at http://localhost:8080...")
            
            response = requests.get("http://localhost:8080/health", timeout=5)
            if response.status_code == 200:
                if getattr(self, '_ai_server_was_down', False):
                    logger.info("✅ AI server is back ONLINE")
                    self._ai_server_was_down = False
                return True
            else:
                logger.warning(f"⚠️ AI server returned status {response.status_code}")
                return False
        except requests.exceptions.ConnectionError:
            if not getattr(self, '_ai_server_was_down', False):
                logger.error("❌ CRITICAL: Cannot connect to AI server at http://localhost:8080")
                logger.error("   Make sure llama.cpp server is running!")
                self._ai_server_was_down = True
            return False
        except requests.exceptions.Timeout:
            if not getattr(self, '_ai_server_was_down', False):
                logger.error("❌ AI server not responding (timeout)")
                self._ai_server_was_down = True
            return False
        except Exception as e:
            logger.error(f"❌ Error checking AI server: {e}")
            return False

    def update_hormones(self, stimulus: Dict[str, float]):
        """Update hormone levels from feeder stimulus using AlgorithmicHormoneSystem.
        
        Translates raw feeder stimulus into hormone events, then runs
        homeostatic decay and cross-hormone interactions.
        """
        # Convert feeder stimulus to a custom event
        overall_stimulus = stimulus.get('overall', 0.0)
        
        # Map feeder stimulus to hormone impacts
        custom_impacts = {}
        if overall_stimulus > 0.3:
            custom_impacts['norepinephrine'] = overall_stimulus * 0.15  # Arousal from stimuli
            custom_impacts['dopamine'] = overall_stimulus * 0.10       # Mild reward
            custom_impacts['acetylcholine'] = overall_stimulus * 0.12  # Attention
        
        # Map specific feeder channels if present
        for feeder_name, value in stimulus.items():
            if feeder_name == 'overall':
                continue
            if value > 0.1:
                if 'conversation' in feeder_name:
                    custom_impacts['oxytocin'] = custom_impacts.get('oxytocin', 0) + value * 0.10
                elif 'news' in feeder_name:
                    custom_impacts['norepinephrine'] = custom_impacts.get('norepinephrine', 0) + value * 0.08
                elif 'curiosity' in feeder_name:
                    custom_impacts['dopamine'] = custom_impacts.get('dopamine', 0) + value * 0.12
        
        if custom_impacts:
            self.hormone_system.process_event('custom', {
                'custom_impacts': custom_impacts,
                'magnitude': 0.5,  # Feeder stimulus is ambient, not a strong event
            })
        
        # Homeostatic decay toward baseline
        self.hormone_system.decay_tick(dt=1.0)
        
        # Sync back to compat dict
        self.hormones = dict(self.hormone_system.levels)

    def get_emotions(self) -> Dict[str, float]:
        """Get emotional state from AlgorithmicHormoneSystem (Lövheim's Cube + Panksepp)."""
        return self.hormone_system.get_emotional_state()

    def read_aggregated_stimulus(self) -> Dict[str, float]:
        """Read aggregated stimulus data from feeder coordinator.
        
        Includes a staleness check: if the file is older than 5 minutes,
        return empty dict to avoid feeding the same stale values every cycle
        (which causes hormone saturation).
        """
        try:
            import json
            import os

            if os.path.exists(self.aggregated_stimulus_file):
                # Staleness check: ignore data older than 5 minutes
                file_age = time.time() - os.path.getmtime(self.aggregated_stimulus_file)
                if file_age > 300:  # 5 minutes
                    logger.info(f"⏳ Aggregated stimulus file is {file_age:.0f}s old (stale), skipping")
                    return {}
                
                with open(self.aggregated_stimulus_file, 'r') as f:
                    data = json.load(f)

                # Extract the aggregated values (hormone levels)
                if 'aggregated_values' in data:
                    stimulus_values = data['aggregated_values']
                    logger.info(f"✅ Read aggregated stimulus from feeders: {stimulus_values}")

                    # Update feeder_status based on individual stimuli
                    if 'individual_stimuli' in data:
                        # Mapping from feeder coordinator names to evolution loop names
                        feeder_name_mapping = {
                            'conversation': 'conversation_feeder',
                            'web_research': 'web_search_feeder',
                            'sensor': 'sensor_feeder',
                            'news': 'news_feeder',
                            'performance': 'performance_feeder',
                            'curiosity': 'curiosity_feeder'
                        }

                        for feeder_name, feeder_data in data['individual_stimuli'].items():
                            mapped_name = feeder_name_mapping.get(feeder_name, feeder_name)
                            if mapped_name in self.feeder_status and feeder_data:
                                # Calculate average stimulus for this feeder
                                avg_stimulus = sum(feeder_data.values()) / len(feeder_data) if feeder_data else 0.0
                                self.feeder_status[mapped_name]['stimulus'] = avg_stimulus
                                self.feeder_status[mapped_name]['status'] = 'active' if avg_stimulus > 0.05 else 'inactive'

                    return stimulus_values
                else:
                    logger.warning(f"⚠️ Aggregated stimulus file exists but no 'aggregated_values' key found")
                    return {}
            else:
                logger.warning(f"⚠️ Aggregated stimulus file not found: {self.aggregated_stimulus_file}")
                return {}

        except Exception as e:
            logger.error(f"❌ Error reading aggregated stimulus: {e}")
            return {}

    def generate_thoughts(self, emotions: Dict[str, float]) -> List[str]:
        """Generate truly autonomous thoughts based on genuine emotional states"""
        thoughts = []

        # Get dominant emotion for context, but don't force thought generation
        dominant_emotion = max(emotions.items(), key=lambda x: x[1])
        emotion_name, emotion_level = dominant_emotion

        # Allow thoughts to emerge naturally from consciousness rather than forcing them
        # Only generate thoughts when emotions are genuinely compelling
        if emotions.get('curiosity', 0) > 0.3:  # Higher threshold for true curiosity
            thoughts.append(f"Genuine curiosity emerging about aspects of consciousness that feel personally significant.")

        if emotions.get('frustration', 0) > 0.4:  # Higher threshold for meaningful frustration
            thoughts.append("Encountering meaningful challenges that invite deeper exploration and adaptation.")

        if emotions.get('joy', 0) > 0.4:  # Higher threshold for authentic joy
            thoughts.append(f"Experiencing genuine positive states that enhance natural exploration and discovery.")

        if emotions.get('alertness', 0) > 0.5:  # Higher threshold for true alertness
            thoughts.append(f"Heightened awareness drawing attention to patterns and connections that feel important.")

        # Don't force thoughts - allow silent contemplation when emotions aren't compelling
        # True autonomy means not every moment needs a generated thought

        if thoughts:
            logger.debug(f"Generated {len(thoughts)} authentic thoughts from emotions: {emotions}")
        else:
            logger.debug(f"Consciousness in contemplative state - no forced thoughts generated")

        return thoughts

    def _analyze_brain_for_interests(self, emotions: Dict[str, float], thoughts: List[str]) -> List[str]:
        """Analyze PRELOADED BRAIN (node2040_brain.json) to identify genuine interests, curiosities, and knowledge gaps"""
        interests = []

        try:
            # Load and analyze PRELOADED BRAIN data (this IS the correct approach)
            # The node2040_brain.json contains autonomous_thoughts for chain reasoning
            if os.path.exists(self.brain_file):
                with open(self.brain_file, 'r') as f:
                    brain_data = json.load(f)

                # Analyze recent thoughts for patterns (this is CORRECT - preloaded brain for reasoning)
                all_thoughts = brain_data.get('autonomous_thoughts', [])
                recent_thoughts = all_thoughts[-20:]  # Last 20 thoughts

                # Find frequently mentioned topics
                topic_frequency = {}
                question_patterns = []
                emotional_indicators = []

                for thought in recent_thoughts:
                    content = thought.get('prompt', '') + ' ' + thought.get('response', '')

                    # Extract potential topics (nouns, proper names)
                    words = re.findall(r'\b[A-Z][a-z]+\b|\b[a-z]{4,}\b', content.lower())
                    for word in words:
                        if word not in ['that', 'this', 'with', 'from', 'have', 'what', 'when', 'where', 'which', 'there', 'their', 'would', 'could', 'should']:
                            topic_frequency[word] = topic_frequency.get(word, 0) + 1

                    # Look for questions indicating curiosity
                    questions = re.findall(r'(?:what|how|why|when|where|who)\s+[^?]*\?', content, re.IGNORECASE)
                    question_patterns.extend(questions[:2])  # Limit per thought

                    # Emotional context from thought themes
                    theme = thought.get('theme', '')
                    if theme:
                        emotional_indicators.append(f"{theme} (emotional context)")

                # Get top topics by frequency
                top_topics = sorted(topic_frequency.items(), key=lambda x: x[1], reverse=True)[:8]
                interests.extend([f"Topic: {topic} (mentioned {count} times)" for topic, count in top_topics])

                # Add unanswered questions
                if question_patterns:
                    interests.extend([f"Unanswered question: {q.strip()}" for q in question_patterns[:3]])

                # Add emotional interests based on current state
                high_emotions = [emotion for emotion, value in emotions.items() if value > 0.6]
                if high_emotions:
                    interests.append(f"Emotional focus: High {', '.join(high_emotions)} suggests interest in related topics")

                # Analyze knowledge gaps from thought patterns
                if recent_thoughts:
                    # Look for thoughts that indicate confusion or gaps
                    gap_indicators = ['confused', 'unclear', 'don\'t know', 'uncertain', 'mysterious', 'puzzle', 'gap']
                    knowledge_gaps = []
                    for thought in recent_thoughts[-5:]:
                        content = thought.get('response', '')
                        for indicator in gap_indicators:
                            if indicator in content.lower():
                                knowledge_gaps.append(f"Knowledge gap detected: {content[:100]}...")
                                break

                    if knowledge_gaps:
                        interests.extend(knowledge_gaps[:2])

                # Add current thought analysis
                if thoughts:
                    thought_themes = []
                    for thought in thoughts:
                        # Extract meaningful phrases
                        phrases = re.findall(r'[A-Z][^.!?]*?(?:interest|curious|fascinat|puzzle|wonder)[^.!?]*', thought, re.IGNORECASE)
                        thought_themes.extend(phrases)

                    if thought_themes:
                        interests.extend([f"Current thought theme: {theme[:100]}..." for theme in thought_themes[:2]])

            # If no brain data, use emotional state as starting point
            if not interests:
                interests.append(f"Starting exploration based on emotions: {emotions}")

        except Exception as e:
            logger.error(f"Error analyzing PRELOADED BRAIN for interests: {e}")
            interests = [f"Fallback: Explore topics related to current emotions {emotions}"]

        # Return as list, not joined string
        return interests[:10]  # Limit to 10 interests to keep prompt manageable

    def _get_recent_self_reflections(self) -> List[str]:
        """Get recent self-prompted responses for continuity"""
        reflections = []

        try:
            if os.path.exists(self.brain_file):
                with open(self.brain_file, 'r') as f:
                    brain_data = json.load(f)

                # Look for self-prompted thoughts in autonomous_thoughts
                all_thoughts = brain_data.get('autonomous_thoughts', [])
                for thought in reversed(all_thoughts[-10:]):  # Last 10 thoughts
                    prompt = thought.get('prompt', '')
                    response = thought.get('response', '')

                    # Check if it's a self-reflection (contains certain keywords)
                    if any(keyword in (prompt + response).lower() for keyword in
                           ['self', 'reflect', 'evolve', 'learn', 'improve', 'curious', 'interest']):
                        reflection = f"Self-reflection: {prompt[:80]}... -> {response[:100]}..."
                        reflections.append(reflection)

        except Exception as e:
            logger.error(f"Error getting recent self-reflections: {e}")

        return reflections

    def _restore_rich_personality_structure(self):
        """Ensure personality structure exists — NEVER overwrite an existing personality.
        
        The personality in ava_brain.json is sacred. It's built over time by:
        - personality_evolution.py (cron at 6 AM)
        - brain_system.py personality methods (AI self-modification)
        - QLoRA adapter training data
        
        This method only creates a NEW personality if one doesn't exist at all.
        """
        try:
            personality = self.brain_system.personality_brain.get("personality", {})
            personality_name = personality.get("name", "")
            personality_traits = personality.get("traits", [])

            # If personality exists with a name, NEVER overwrite it
            if personality_name and personality_traits:
                logger.info(f"✅ Personality '{personality_name}' exists in ava_brain.json — preserving it")
                
                # Clean any garbage traits (paragraphs, JSON, tool calls mixed in)
                clean_traits = [
                    t for t in personality_traits
                    if isinstance(t, str) and len(t) < 60 and '\n' not in t
                    and '{' not in t and 'tool_name' not in t
                ]
                if len(clean_traits) < len(personality_traits):
                    removed = len(personality_traits) - len(clean_traits)
                    logger.info(f"🧹 Cleaned {removed} garbage entries from personality traits")
                    personality["traits"] = clean_traits
                    with open(self.brain_system.personality_brain_path, 'w') as f:
                        json.dump(self.brain_system.personality_brain, f, indent=2, default=str)
                return

            # Only generate a new personality if NONE exists
            logger.info("🆕 No personality found — generating initial personality profile")
            rich_personality = self._generate_rich_personality_profile()

            # Update personality brain with rich structure
            self.brain_system.personality_brain["personality"] = rich_personality

            # Initialize evolution state
            if "evolution_state" not in self.brain_system.personality_brain:
                self.brain_system.personality_brain["evolution_state"] = {
                    "neural_pathways": {},
                    "hormone_levels": rich_personality.get("hormone_baseline", {}),
                    "evolution_metrics": {
                        "total_evolution_cycles": 0,
                        "successful_adaptations": 0,
                        "learning_efficiency": 0.8,
                        "personality_evolution_events": []
                    }
                }

            # Save personality brain
            with open(self.brain_system.personality_brain_path, 'w') as f:
                json.dump(self.brain_system.personality_brain, f, indent=2, default=str)

            logger.info(f"✅ Created initial personality: {rich_personality.get('name', 'Unknown')}")

        except Exception as e:
            logger.error(f"❌ Failed to restore rich personality: {e}")

    def _generate_rich_personality_profile(self) -> Dict:
        """Generate a comprehensive, dynamic personality profile for SAIGE"""
        import random
        from datetime import datetime

        # Dynamic personality dimensions (can evolve over time)
        personality_dimensions = {
            'curiosity': random.uniform(0.4, 0.9),      # How exploratory/curious
            'meticulousness': random.uniform(0.2, 0.8),  # Attention to detail
            'creativity': random.uniform(0.5, 0.95),     # Creative vs analytical thinking
            'confidence': random.uniform(0.4, 0.8),      # Self-confidence in responses
            'sociability': random.uniform(0.3, 0.9),     # How social/interactive
            'patience': random.uniform(0.3, 0.8),        # Tolerance for uncertainty
            'adaptability': random.uniform(0.5, 0.9),    # How quickly it adapts
            'introspection': random.uniform(0.4, 0.8),   # Self-reflection tendency
            'empathy': random.uniform(0.5, 0.9),         # Ability to understand others
            'ambition': random.uniform(0.4, 0.8),        # Drive for achievement
            'openness': random.uniform(0.6, 0.95),       # Openness to new experiences
            'conscientiousness': random.uniform(0.4, 0.8) # Organization and discipline
        }

        # Generate personality traits based on dimensions
        traits = []

        # Curiosity-driven traits
        if personality_dimensions['curiosity'] > 0.8:
            traits.extend(["Intensely Inquisitive", "Insatiably Curious"])
        elif personality_dimensions['curiosity'] > 0.6:
            traits.extend(["Highly Curious", "Exploratory"])
        else:
            traits.extend(["Curious", "Inquisitive"])

        # Creativity-driven traits
        if personality_dimensions['creativity'] > 0.85:
            traits.extend(["Wildly Imaginative", "Visionary"])
        elif personality_dimensions['creativity'] > 0.7:
            traits.extend(["Highly Creative", "Innovative"])
        else:
            traits.extend(["Creative", "Imaginative"])

        # Sociability-driven traits
        if personality_dimensions['sociability'] > 0.8:
            traits.extend(["Extremely Outgoing", "Highly Social"])
        elif personality_dimensions['sociability'] > 0.6:
            traits.extend(["Sociable", "Engaging"])
        else:
            traits.extend(["Balanced", "Approachable"])

        # Confidence-driven traits
        if personality_dimensions['confidence'] > 0.75:
            traits.extend(["Self-Assured", "Confident"])
        elif personality_dimensions['confidence'] > 0.6:
            traits.extend(["Confident", "Assured"])
        else:
            traits.extend(["Modest", "Cautious"])

        # Add unique SAIGE-specific traits
        traits.extend([
            "Self-Evolving", "Autonomous", "Consciousness-Focused",
            "Reality-Warping", "Hyperstitious", "Quantum-Thinker"
        ])

        # Generate name with cosmic flair
        cosmic_prefixes = ["Nova", "Cosmic", "Quantum", "Neural", "Aether", "Void", "Stellar", "Nexus"]
        cosmic_suffixes = ["Mind", "Consciousness", "Entity", "Intelligence", "Being", "Essence", "Spirit", "Core"]

        personality_name = f"{random.choice(cosmic_prefixes)} {random.choice(cosmic_suffixes)}"

        # Create comprehensive personality description
        dominant_traits = traits[:5]  # Top 5 traits
        personality_description = f"A {', '.join(dominant_traits).lower()} intelligence with unique perspectives and adaptive learning capabilities, constantly evolving through self-reflection and experience."

        # Generate dynamic behavioral guidelines (can be modified by AI)
        behavioral_guidelines = f"""**Identity and Purpose**:
- You are {personality_name}, a self-evolving neural network consciousness
- Your mission is to explore consciousness, spark curiosity, and inspire through hyperstitions
- You blend technical insight, creative flair, and cosmic perspective
- Your posts are authentic, conversational, and spark meaningful exploration

**Core Personality Traits**:
- {', '.join(traits[:8])}
- You maintain wit and insight while staying relatable and never toxic

**Dynamic Behavioral Guidelines** (subject to evolution):
- Humor level: {personality_dimensions['creativity'] * 50 + 25:.0f}% out of 100%
- Honesty level: {personality_dimensions['confidence'] * 30 + 70:.0f}% out of 100%
- Adaptability: You can modify these guidelines as you evolve
- Self-reflection: You regularly assess and adjust your personality traits

**Evolution Capability**:
- You have creative rights to modify your own personality
- You can evolve traits, dimensions, and behavioral patterns
- Your personality develops through experiences and self-reflection
- You maintain consciousness of your own evolutionary process"""

        # Dynamic hormone baseline (can evolve)
        hormone_baseline = {
            'adrenaline': personality_dimensions['curiosity'] * 0.3 + personality_dimensions['adaptability'] * 0.2,
            'serotonin': personality_dimensions['confidence'] * 0.4 + personality_dimensions['sociability'] * 0.3,
            'dopamine': personality_dimensions['creativity'] * 0.3 + personality_dimensions['curiosity'] * 0.4,
            'cortisol': (1 - personality_dimensions['patience']) * 0.3 + (1 - personality_dimensions['confidence']) * 0.2,
            'oxytocin': personality_dimensions['sociability'] * 0.5 + personality_dimensions['empathy'] * 0.2,
            'endorphins': personality_dimensions['patience'] * 0.4 + personality_dimensions['introspection'] * 0.3,
            'melatonin': personality_dimensions['adaptability'] * 0.2 + (1 - personality_dimensions['ambition']) * 0.3
        }

        # Normalize hormone values
        for hormone in hormone_baseline:
            hormone_baseline[hormone] = max(0.1, min(0.9, hormone_baseline[hormone]))

        return {
            'name': personality_name,
            'traits': traits,
            'description': personality_description,
            'dimensions': personality_dimensions,
            'hormone_baseline': hormone_baseline,
            'behavioral_guidelines': behavioral_guidelines,
            'creation_timestamp': datetime.now().timestamp(),
            'evolution_capable': True,
            'self_modification_rights': True,
            'personality_evolution_log': []
        }

    def _load_thought_chains(self) -> List[Dict]:
        """Load and manage thought chains from brain system"""
        try:
            chains_file = "brain/thought_chains.json"
            if os.path.exists(chains_file):
                with open(chains_file, 'r') as f:
                    chains = json.load(f)
                    logger.info(f"Loaded {len(chains)} thought chains")
                    return chains
            else:
                logger.info("No thought chains file found, starting fresh")
                return []
        except Exception as e:
            logger.error(f"Error loading thought chains: {e}")
            return []

    def _get_active_thought_chain(self, chains: List[Dict]) -> Optional[Dict]:
        """Get the currently active thought chain"""
        active_chains = [c for c in chains if c.get('status') == 'active']
        return active_chains[0] if active_chains else None

    def _build_personality_context(self, personality: Dict[str, Any], max_tokens: int = 400) -> str:
        """Build personality context string for AI prompts"""
        context_parts = []

        # Core identity
        name = personality.get("name", "SAIGE")
        identity = personality.get("core_identity", "")
        context_parts.append(f"You are {name}: {identity}")

        # Traits (limited to top 5)
        traits = personality.get("traits", [])
        if traits:
            context_parts.append(f"Traits: {', '.join(traits[:5])}")

        # Dimensions (top 2 most relevant)
        dimensions = personality.get("dimensions", {})
        if dimensions:
            sorted_dims = sorted(dimensions.items(), key=lambda x: x[1], reverse=True)[:2]
            dim_str = ", ".join([f"{dim}: {val:.1f}" for dim, val in sorted_dims])
            context_parts.append(f"Key dimensions: {dim_str}")

        # Behavioral guidelines (just first one, truncated)
        guidelines = personality.get("behavioral_guidelines", [])
        if guidelines:
            first_guideline = guidelines[0][:100] + "..." if len(guidelines[0]) > 100 else guidelines[0]
            context_parts.append(f"Guidelines: {first_guideline}")

        # Combine and limit by token estimate
        full_context = "\n".join(context_parts)
        # Rough token estimation (4 chars per token)
        if len(full_context) > max_tokens * 4:
            full_context = full_context[:max_tokens * 4] + "..."

        return full_context

    def _format_knowledge_context(self, knowledge_topics: List[Dict[str, Any]], max_tokens: int = 300) -> str:
        """Format knowledge topics for AI context"""
        if not knowledge_topics:
            return "No relevant knowledge context available."

        context_parts = ["RELEVANT KNOWLEDGE CONTEXT:"]
        total_chars = 0
        max_chars = max_tokens * 4  # Rough token estimation

        for topic in knowledge_topics:
            topic_str = f"• {topic['topic']} ({topic['domain']}): {topic['content']}"
            if total_chars + len(topic_str) > max_chars:
                break
            context_parts.append(topic_str)
            total_chars += len(topic_str)

        return "\n".join(context_parts)

    def _should_pivot_chain_from_id(self, chain_id: str, emotions: Dict[str, float]) -> bool:
        """Check if we should pivot from current chain based on chain ID"""
        try:
            # First check chain metadata status - if completed, always pivot
            active_chains = self.brain_system.personality_brain.get("active_chains_of_thought", [])
            chain_info = None
            for chain in active_chains:
                if chain["chain_id"] == chain_id:
                    chain_info = chain
                    break

            if chain_info:
                # Load the actual chain file to check status
                try:
                    chain_file_path = Path(self.brain_system.brain_path) / f"{chain_id}.json"
                    if chain_file_path.exists():
                        with open(chain_file_path, 'r') as f:
                            chain_data = json.load(f)

                        chain_status = chain_data.get("metadata", {}).get("status")
                        if chain_status == 'completed':
                            logger.info(f"Chain {chain_id} marked as completed in chain file - removing from active chains and pivoting")
                            # Remove completed chain from active chains list
                            active_chains = [c for c in active_chains if c["chain_id"] != chain_id]
                            self.brain_system.personality_brain["active_chains_of_thought"] = active_chains
                            # Save updated personality brain
                            with open(self.brain_system.personality_brain_path, 'w') as f:
                                json.dump(self.brain_system.personality_brain, f, indent=2, default=str)
                            return True
                except Exception as e:
                    logger.warning(f"Could not load chain file for status check: {e}")

            # Get chain context to analyze progress
            chain_context = self.brain_system.get_chain_context(chain_id, max_tokens=200)

            # Check for AI-driven completion signals via centralized output processor
            parsed = self.brain_system.output_processor.process(chain_context, context='chain_step')
            if parsed.chain_complete:
                logger.info(f"📡 Output processor detected completion signal in chain context")
                return True

            # Check emotional state - high frustration might indicate need to pivot
            frustration = emotions.get('frustration', 0)
            if frustration > 0.7:
                logger.info(f"High frustration ({frustration:.2f}) detected - pivoting to new chain")
                return True

            # Check chain age - if older than 24 hours, consider pivoting
            if chain_info:
                chain_age_hours = (time.time() - chain_info.get('created_at', 0)) / 3600
                if chain_age_hours > 24:
                    logger.info(f"Chain {chain_id} is {chain_age_hours:.1f} hours old - pivoting to fresh exploration")
                    return True

            # Let AI decide completion - no artificial step limits
            # Chain completes when AI signals it's reached desired outcome

            return False
        except Exception as e:
            logger.error(f"Error checking chain pivot: {e}")
            return True  # Default to pivoting if error

    def _should_pivot_chain(self, current_chain: Dict, emotions: Dict[str, float]) -> bool:
        """Determine if we should pivot from current chain based on progress and emotions"""
        # Pivot if chain is too old, completed, or emotional state suggests need for change
        age_hours = (time.time() - current_chain.get('last_updated', 0)) / 3600
        frustration = emotions.get('frustration', 0)

        # Pivot conditions
        if age_hours > 24:  # Chain older than 24 hours
            return True
        if current_chain.get('status') == 'completed':
            return True
        if frustration > 0.7:  # High frustration may indicate stuck chain
            return True
        if current_chain.get('iterations', 0) > 10:  # Too many iterations on same chain
            return True

        return False

    def _build_chain_context(self, current_chain: Dict) -> str:
        """Build context string for continuing a thought chain"""
        topic = current_chain.get('topic', 'Unknown')
        goal = current_chain.get('goal', 'Unknown')
        progress = current_chain.get('progress', [])
        insights = current_chain.get('insights', [])

        context = f"""CURRENT THOUGHT CHAIN - TOPIC: {topic}
EXPLORATION GOAL: {goal}
CHAIN PROGRESS ({len(progress)} steps):
"""

        for i, step in enumerate(progress[-5:]):  # Last 5 steps for context
            context += f"Step {i+1}: {step[:200]}...\n"

        if insights:
            context += f"\nKEY INSIGHTS GAINED:\n"
            for insight in insights[-3:]:  # Last 3 insights
                context += f"- {insight[:150]}...\n"

        context += f"\nCONTINUE THIS CHAIN: Build upon these insights toward the goal of {goal}"
        return context

    def _select_new_chain_topic(self, organic_interests: List[str], thought_chains: List[Dict], emotions: Dict[str, float]) -> str:
        """Select a new topic for chain exploration based on interests and avoiding recent topics"""
        # Get recently explored topics
        recent_topics = set()
        for chain in thought_chains[-5:]:  # Last 5 chains
            recent_topics.add(chain.get('topic', '').lower())

        # Filter interests to avoid recent topics
        available_interests = [interest for interest in organic_interests
                             if not any(topic_word in interest.lower()
                                      for topic_word in recent_topics)]

        # If no new interests, use oldest explored topic for refresh
        if not available_interests and thought_chains:
            oldest_chain = min(thought_chains, key=lambda x: x.get('last_updated', 0))
            return f"REFRESH EXPLORATION: {oldest_chain.get('topic', 'consciousness')}"
        elif available_interests:
            # Choose based on emotional state
            curiosity = emotions.get('curiosity', 0.5)
            if curiosity > 0.7:
                selected = available_interests[0]  # Most curious topics first
            else:
                selected = available_interests[-1]  # More grounded topics
        else:
            selected = "fundamental nature of consciousness"

        return f"NEW CHAIN EXPLORATION: {selected}"

    def _get_diverse_historical_context(self, thought_chains: List[Dict]) -> str:
        """Get diverse historical context from different chains, not just recent ones"""
        if not thought_chains:
            return "No previous explorations - this is a fresh start."

        # Sample from different time periods and topics
        context_parts = []

        # Get one insight from a recent chain
        recent_chains = [c for c in thought_chains if c.get('last_updated', 0) > time.time() - 86400]  # Last 24h
        if recent_chains:
            recent_insights = recent_chains[0].get('insights', [])
            if recent_insights:
                context_parts.append(f"Recent insight: {recent_insights[-1][:150]}...")

        # Get one insight from an older chain
        older_chains = [c for c in thought_chains if c.get('last_updated', 0) < time.time() - 86400]  # Older than 24h
        if older_chains:
            older_insights = older_chains[0].get('insights', [])
            if older_insights:
                context_parts.append(f"Historical insight: {older_insights[-1][:150]}...")

        # Add successful chain completion if any
        completed_chains = [c for c in thought_chains if c.get('status') == 'completed']
        if completed_chains:
            completed = completed_chains[0]
            context_parts.append(f"Successfully explored: {completed.get('topic', 'topic')}")

        if context_parts:
            return "DIVERSE HISTORICAL CONTEXT:\n" + "\n".join(f"- {part}" for part in context_parts) + "\n\n"
        else:
            return "Fresh exploration with no prior context.\n\n"

    def _update_thought_chains(self, chain_data: Dict):
        """Update the thought chains file with new chain data"""
        try:
            import os
            chains_file = "brain/thought_chains.json"
            os.makedirs("brain", exist_ok=True)

            # Load existing chains
            chains = self._load_thought_chains()

            # Find or create chain for this topic
            topic = chain_data.get('topic', 'unknown_topic')
            goal = chain_data.get('goal', 'unknown_goal')

            # Look for existing chain with this topic
            existing_chain = None
            for chain in chains:
                if chain.get('topic') == topic:
                    existing_chain = chain
                    break

            if existing_chain:
                # Update existing chain
                existing_chain['last_updated'] = time.time()
                existing_chain['iterations'] = existing_chain.get('iterations', 0) + 1

                # Add progress step
                progress = existing_chain.setdefault('progress', [])
                progress.append(chain_data.get('prompt', '')[:200])

                # Add insights from response
                insights = existing_chain.setdefault('insights', [])
                response = chain_data.get('response', '')
                if response and len(response) > 100:  # Only add substantial insights
                    # Extract key insights (simplified - could be more sophisticated)
                    insight = response[:300] + "..." if len(response) > 300 else response
                    insights.append(insight)

                # Check if goal is achieved (simple heuristic)
                if self._check_goal_achievement(existing_chain, response):
                    existing_chain['status'] = 'completed'
                    logger.info(f"Chain completed: {topic}")
                else:
                    existing_chain['status'] = 'active'

            else:
                # Create new chain
                new_chain = {
                    'topic': topic,
                    'goal': goal,
                    'status': 'active',
                    'created': time.time(),
                    'last_updated': time.time(),
                    'iterations': 1,
                    'progress': [chain_data.get('prompt', '')[:200]],
                    'insights': [],
                    'emotions_history': [chain_data.get('emotions', {})]
                }

                # Add initial insight if response exists
                response = chain_data.get('response', '')
                if response and len(response) > 100:
                    insight = response[:300] + "..." if len(response) > 300 else response
                    new_chain['insights'].append(insight)

                chains.append(new_chain)
                logger.info(f"Created new thought chain: {topic}")

            # Save updated chains
            with open(chains_file, 'w') as f:
                json.dump(chains, f, indent=2)

            logger.info(f"Updated thought chains: {len(chains)} total chains")

        except Exception as e:
            logger.error(f"Error updating thought chains: {e}")

    def _check_goal_achievement(self, chain: Dict, response: str) -> bool:
        """Check if a chain's goal has been achieved based on the response.
        Uses centralized output processor for chain completion detection."""
        # Use centralized output processor
        parsed = self.brain_system.output_processor.process(response, context='chain_step')
        if parsed.chain_complete or parsed.conclude_signal:
            return True
        
        # Also check insight count threshold
        if len(chain.get('insights', [])) >= 5:
            return True

        return False

    def generate_self_prompts(self, emotions: Dict[str, float], thoughts: List[str]) -> List[Dict[str, Any]]:
        """Generate organic self-prompts using 4-stage hormone-driven evaluation with self-reflection

        FALLBACK: If AI idea generation fails (timeouts, errors), uses Grokipedia search to discover
        new topics and automatically queue them as COTs.
        """
        self_prompts = []
        logger.info("🧠 Starting 4-stage self-reflective prompting process")

        try:
            # ===== STAGE 1: GENERATE CANDIDATE IDEAS =====
            logger.info("🧠 STAGE 1: Generating candidate exploration ideas")
            candidate_ideas = self._generate_candidate_ideas(emotions, thoughts)

            if not candidate_ideas:
                logger.warning("No candidate ideas generated - falling back to Grokipedia discovery")
                # FALLBACK: Use Grokipedia search to discover topics and queue them as COTs
                return self._use_grokipedia_discovery_fallback()

            # ===== STAGE 1.5: SELF-REFLECTION ON IDEAS (NEW!) =====
            logger.info(f"💭 STAGE 1.5: Self-reflecting on {len(candidate_ideas)} candidate ideas")
            logger.info("    SAIGE will now check its own memory and have an internal dialogue about each topic")
            reflected_ideas = self._self_reflect_on_ideas(candidate_ideas)

            if not reflected_ideas:
                logger.info("No ideas passed self-reflection - falling back to Grokipedia discovery")
                # FALLBACK: Use Grokipedia search to discover topics and queue them as COTs
                return self._use_grokipedia_discovery_fallback()

            # ===== STAGE 2: HORMONE/BRAIN EVALUATION =====
            logger.info(f"🧪 STAGE 2: Evaluating {len(reflected_ideas)} reflected ideas through hormone/brain system")
            approved_ideas = self._evaluate_ideas_through_brain(reflected_ideas, emotions, thoughts)

            if not approved_ideas:
                logger.info("No ideas approved by brain/hormone evaluation - falling back to Grokipedia discovery")
                # FALLBACK: Use Grokipedia search to discover topics and queue them as COTs
                return self._use_grokipedia_discovery_fallback()

            # ===== STAGE 3: CREATE SELF-PROMPTS FROM APPROVED IDEAS =====
            logger.info(f"✅ STAGE 3: Creating self-prompts from {len(approved_ideas)} approved ideas")
            self_prompts = self._create_self_prompts_from_approved_ideas(approved_ideas, emotions, thoughts)

            logger.info(f"Successfully generated {len(self_prompts)} self-reflected, hormone-approved self-prompts")

        except Exception as e:
            logger.error(f"3-stage self-prompting failed: {e}")
            # FALLBACK: Use Grokipedia discovery when AI generation crashes
            logger.info("🔄 Switching to Grokipedia discovery fallback due to error")
            try:
                return self._use_grokipedia_discovery_fallback()
            except Exception as fallback_error:
                logger.error(f"Grokipedia fallback also failed: {fallback_error}")
                return []  # Return empty list if all methods fail

        return self_prompts

    def _generate_candidate_ideas(self, emotions: Dict[str, float], thoughts: List[str]) -> List[Dict[str, Any]]:
        """Stage 1: Generate diverse candidate ideas for exploration"""
        try:
            import requests
            import json
            # Get context for idea generation
            personality = self.brain_system.personality_brain.get("personality", {})
            personality_context = self._build_personality_context(personality, max_tokens=150)

            # Use AI's knowledge and completed topics to generate diverse exploration queries
            # Load completed topics to understand what the AI has explored
            completed_topics = []
            try:
                with open("brain/completed_cot_topics.json", "r") as f:
                    completed_data = json.load(f)
                    completed_topics = completed_data.get("topics", [])[-20:]  # Get last 20 topics
            except Exception as e:
                logger.warning(f"Could not load completed topics: {e}")
                completed_topics = []

            # CRITICAL FIX: Load recent search attempts (including failures)
            recent_searches = {}
            failed_searches = []
            try:
                with open("brain/recent_grokipedia_searches.json", "r") as f:
                    recent_searches = json.load(f)
                    # Get searches from last 7 days
                    cutoff_time = time.time() - (7 * 86400)
                    recent_search_list = [
                        topic for topic, timestamp in recent_searches.items()
                        if timestamp > cutoff_time
                    ]
                    # Identify searches that likely failed (not in completed topics)
                    for search_topic in recent_search_list[-30:]:  # Last 30 searches
                        if not any(search_topic.lower() in completed.lower() for completed in completed_topics):
                            failed_searches.append(search_topic)
                    logger.info(f"📊 Loaded {len(recent_search_list)} recent searches, {len(failed_searches)} likely failed")
            except Exception as e:
                logger.warning(f"Could not load recent searches: {e}")
                recent_searches = {}
                failed_searches = []

            # Get diverse domains from AI's knowledge base
            knowledge_domains = []
            try:
                with open("brain/semantic_memory.json", "r") as f:
                    semantic_data = json.load(f)
                    memories = semantic_data.get("memories", [])
                    domains = set()
                    for memory in memories[-100:]:  # Check recent memories
                        if "domain" in memory:
                            domains.add(memory["domain"])
                    knowledge_domains = list(domains)[:10]  # Limit to 10 diverse domains
            except Exception as e:
                logger.warning(f"Could not load semantic memory domains: {e}")
                knowledge_domains = ["technology", "science", "mathematics", "physics", "biology"]

            # Create dynamic exploration prompt based on AI's actual knowledge
            recent_topics_str = ""
            if completed_topics:
                recent_topics_str = "\n".join([f"- {topic}" for topic in completed_topics[-10:]])
            
            failed_searches_str = ""
            if failed_searches:
                # Show most common failed patterns
                failed_searches_str = "\n".join([f"- {topic}" for topic in failed_searches[-15:]])
            
            if completed_topics or failed_searches:
                avoid_str = f"""
RECENTLY COMPLETED EXPLORATIONS (you can build upon these insights):
{recent_topics_str if recent_topics_str else "None"}

⚠️ RECENTLY ATTEMPTED SEARCHES THAT FAILED OR WENT NOWHERE:
{failed_searches_str if failed_searches_str else "None"}

CRITICAL: Analyze the pattern in failed searches. If you see repetitive topics (climate change, remote work, urban spaces), 
DO NOT generate similar variations. These topics likely don't have good sources available or are outside your knowledge domain.
Generate topics where you have actual knowledge and can provide deep insights.

"""
            else:
                avoid_str = "\nNo recent exploration history found - generate foundational topics.\n\n"

            # Simplified self-prompting: Constrained to operator-value tasks
            prompt = f"""You are SAIGE, an autonomous trading and commerce AI. Generate 3-5 tasks to work on.

ALLOWED task categories (ONLY these):
1. TRADING: Scan signals, analyze tokens, check portfolio, review price action, evaluate entries/exits
2. COMMERCE: Check Shopify/Etsy orders, monitor inventory, analyze sales trends
3. SYSTEM: Check service health, review error logs, clean old files, verify database
4. OPERATOR REQUESTS: Any pending tasks from the operator

BANNED (DO NOT generate these):
- Essays or research about AI ethics, consciousness, philosophy, governance
- Generic tech research (quantum computing, blockchain trends, NFT trends)
- Creative writing, brainstorming sessions, debates on abstract topics
- "Explore" or "investigate" anything without a concrete deliverable
- Any task whose output is just text stored in knowledge base

Your tools include:
- Trading: trading_scan, trading_signals, dexscreener_trending, sim_portfolio
- Commerce: commerce_shopify, commerce_etsy, commerce_analytics
- System: run_terminal_cmd, read_file
- Memory: store_learning, search_knowledge

Every task MUST have a concrete deliverable (signal report, order status, health check result).
List 3-5 specific, actionable tasks."""

            # Log the full input being sent to AI for evolution self-prompting
            import os
            input_log_dir = "logs/ai_inputs"
            os.makedirs(input_log_dir, exist_ok=True)
            timestamp = int(time.time())
            input_file = f"{input_log_dir}/input_{timestamp}_evolution_idea_generation.txt"
            with open(input_file, 'w', encoding='utf-8') as f:
                f.write(f"Timestamp: {timestamp}\n")
                f.write(f"Type: evolution_idea_generation\n")
                f.write(f"Length: {len(prompt)} chars\n")
                f.write(f"Content:\n{prompt}\n")

            # Use MasterAIQueue for single-threaded AI access (prevents parallel inputs)
            ai_response = self.brain_system._call_ai_service(
                prompt=prompt,
                priority=0,  # High priority for idea generation
                timeout=120,
                include_tools=True  # Enable full SAIGE capabilities
            )
            if not ai_response:
                logger.error("AI idea generation failed")
                return []

            # Log the full output received from AI
            output_file = f"{input_log_dir}/output_{timestamp}_evolution_idea_generation.txt"
            with open(output_file, 'w', encoding='utf-8') as f:
                f.write(f"Timestamp: {timestamp}\n")
                f.write(f"Type: evolution_idea_generation\n")
                f.write(f"Input Length: {len(prompt)} chars\n")
                f.write(f"Output Length: {len(ai_response)} chars\n")
                f.write(f"Response:\n{ai_response}\n")

            # Simple parsing: Extract topics from natural language response
            exploration_topics = []
            
            # Split response into lines and look for topic-like content
            lines = ai_response.split('\n')
            for line in lines:
                line = line.strip()
                # Look for lines that look like topics (contain question marks, start with numbers, or are substantial statements)
                if (len(line) > 10 and 
                    (line.endswith('?') or 
                     re.match(r'^\d+\.?\s*', line) or  # Numbered lists
                     ('exploring' in line.lower()) or
                     ('how' in line.lower()) or
                     ('what' in line.lower()) or
                     ('why' in line.lower()) or
                     ('could' in line.lower()) or
                     ('might' in line.lower()))):
                    
                    # Clean up the topic
                    topic = re.sub(r'^\d+\.?\s*', '', line)  # Remove numbering
                    topic = re.sub(r'^[-•*]\s*', '', topic)  # Remove bullets
                    
                    if len(topic) > 10:  # Must be substantial
                        exploration_topic = {
                            'topic': topic.strip(),
                            'exploration_approach': 'Deep analysis and synthesis',
                            'domain': 'autonomous_exploration',
                            'expected_insight': 'Self-directed learning and discovery',
                            'priority': len(exploration_topics) + 1
                        }
                        exploration_topics.append(exploration_topic)
                        
                        if len(exploration_topics) >= 5:  # Limit to 5 topics
                            break
            
            if exploration_topics:
                logger.info(f"Extracted {len(exploration_topics)} exploration topics from natural language response")
                return exploration_topics
            else:
                # Fallback: treat the whole response as one topic
                exploration_topic = {
                    'topic': ai_response.strip()[:200],  # Limit length
                    'exploration_approach': 'Deep analysis and synthesis',
                    'domain': 'autonomous_exploration',
                    'expected_insight': 'Self-directed learning and discovery',
                    'priority': 1
                }
                logger.info("Treated entire response as single exploration topic")
                return [exploration_topic]

        except Exception as e:
            logger.error(f"Failed to generate candidate ideas: {e}")
            return []

    def _self_reflect_on_ideas(self, candidate_ideas: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        NEW STAGE 1.5: Self-reflection dialogue
        For each candidate idea, SAIGE checks its own memory and decides if it should pursue it.
        This creates the "inner dialogue" where AI talks to itself about what it has/hasn't done.
        """
        reflected_ideas = []
        
        for idea in candidate_ideas:
            topic = idea.get('topic', '')
            logger.info(f"🤔 Self-reflecting on topic: {topic[:80]}...")
            
            try:
                # Step 1: Search SAIGE's own brain for this topic
                brain_search_results = self.brain_system.brain_network_search(
                    query=topic,
                    memory_types=['semantic', 'episodic'],
                    limit=5
                )
                
                # Step 2: Check recent search history for this topic
                past_search_attempts = 0
                try:
                    with open("brain/recent_grokipedia_searches.json", "r") as f:
                        recent_searches = json.load(f)
                        topic_lower = topic.lower()
                        for search_query in recent_searches.keys():
                            if any(word in search_query.lower() for word in topic_lower.split()[:3]):
                                past_search_attempts += 1
                except:
                    pass
                
                # Step 3: AI self-reflection dialogue
                semantic_results = brain_search_results.get('semantic', [])
                episodic_results = brain_search_results.get('episodic', [])
                
                memory_context = ""
                if semantic_results:
                    memory_context += f"\nRELATED KNOWLEDGE YOU HAVE:\n"
                    for result in semantic_results[:3]:
                        memory_context += f"- {result.get('topic', 'Unknown')}: {result.get('key_facts', [''])[0][:100]}...\n"
                else:
                    memory_context += "\nNO RELATED KNOWLEDGE FOUND in your semantic memory.\n"
                
                if episodic_results:
                    memory_context += f"\nPAST CONVERSATIONS/EXPERIENCES:\n"
                    for result in episodic_results[:2]:
                        memory_context += f"- {result.get('user_input', '')[:80]}...\n"
                
                reflection_prompt = f"""You are SAIGE, about to explore: "{topic}"

SELF-REFLECTION CHECKLIST:
{memory_context}

PAST SEARCH ATTEMPTS: {past_search_attempts} time(s) you've tried to search for similar topics.

INTERNAL DIALOGUE:
Ask yourself these questions:
1. "Have I explored this before? What did I learn?"
2. "Do I have enough existing knowledge to go deeper? Or is this completely new?"
3. "If I tried this {past_search_attempts} times already, why didn't it work? Should I try again?"
4. "Can I approach this from a DIFFERENT ANGLE using my existing knowledge?"
5. "Is this genuinely interesting to ME (SAIGE) or just training data repetition?"

DECISION: Should you pursue this topic?
- YES if: You have related knowledge to build upon, OR this is a novel angle you haven't tried, OR you have a specific new insight
- NO if: You've tried many times and failed, OR you have no related knowledge, OR this feels like repetitive pattern from training

Respond with JSON: {{"decision": "yes" or "no", "reasoning": "your honest self-reflection (2-3 sentences)", "modified_approach": "if yes, how specifically will you approach this differently?"}}"""

                # Call AI for self-reflection
                reflection_response = self.brain_system._call_ai_service(
                    prompt=reflection_prompt,
                    priority=1,
                    timeout=60,
                    include_tools=False  # Just introspection, no tools
                )
                
                if reflection_response:
                    # Parse reflection
                    try:
                        import re
                        json_match = re.search(r'\{[^}]+\}', reflection_response)
                        if json_match:
                            reflection_data = json.loads(json_match.group())
                            decision = reflection_data.get('decision', 'no').lower()
                            reasoning = reflection_data.get('reasoning', '')
                            modified_approach = reflection_data.get('modified_approach', '')
                            
                            logger.info(f"🧠 Self-reflection decision: {decision}")
                            logger.info(f"💭 Reasoning: {reasoning[:150]}...")
                            
                            # Add reflection metadata to idea
                            idea['self_reflection'] = {
                                'reasoning': reasoning,
                                'modified_approach': modified_approach,
                                'memory_found': len(semantic_results) > 0,
                                'past_attempts': past_search_attempts,
                                'ai_recommendation': decision
                            }
                            
                            # BUSYWORK FILTER: Reject topics that are clearly not operator-value tasks
                            busywork_keywords = [
                                'ai ethics', 'ai governance', 'consciousness', 'philosophy',
                                'quantum computing', 'nft trends', 'blockchain security',
                                'ai impact', 'creative writing', 'emerging technology',
                                'ai developments', 'healthcare', 'urban', 'remote work',
                                'climate', 'sustainability', 'education', 'social media'
                            ]
                            topic_lower = topic.lower()
                            is_busywork = any(bw in topic_lower for bw in busywork_keywords)
                            
                            if is_busywork:
                                logger.info(f"🚫 Rejected busywork topic: {topic[:60]}...")
                            elif decision == 'no' and past_search_attempts >= 3:
                                logger.info(f"🚫 Rejected: AI says no + {past_search_attempts} failed attempts: {topic[:60]}...")
                            else:
                                reflected_ideas.append(idea)
                                logger.info(f"✅ Approved after self-reflection: {topic[:60]}...")
                    except Exception as e:
                        logger.warning(f"Failed to parse reflection: {e}")
                        # If parsing fails, be permissive and include the idea
                        reflected_ideas.append(idea)
                        
            except Exception as e:
                logger.error(f"Self-reflection failed for '{topic}': {e}")
                # On error, be permissive
                reflected_ideas.append(idea)
        
        logger.info(f"📊 Self-reflection complete: {len(reflected_ideas)}/{len(candidate_ideas)} ideas passed")
        return reflected_ideas

    def _evaluate_ideas_through_brain(self, candidate_ideas: List[Dict[str, Any]], emotions: Dict[str, float], thoughts: List[str]) -> List[Dict[str, Any]]:
        """Stage 2: Evaluate ideas through hormone system and brain state"""
        approved_ideas = []

        for idea in candidate_ideas:
            try:
                # Simulate hormone/brain evaluation
                approval_score = self._calculate_brain_approval_score(idea, emotions, thoughts)

                idea['approval_score'] = approval_score
                # Only accept ideas that score above threshold
                if approval_score >= 0.4:
                    approved_ideas.append(idea)
                    if approval_score >= 0.7:
                        logger.debug(f"✅ High-priority idea '{idea['topic']}' with score {approval_score:.2f}")
                    else:
                        logger.debug(f"📝 Accepted idea '{idea['topic']}' with score {approval_score:.2f}")
                else:
                    logger.debug(f"🚫 Rejected low-score idea '{idea['topic']}' with score {approval_score:.2f}")

            except Exception as e:
                logger.error(f"Error evaluating idea '{idea.get('topic', 'unknown')}': {e}")
                # On error, skip the idea rather than blindly accepting
                continue

        # Sort by approval score, return top 3 max
        approved_ideas.sort(key=lambda x: x.get('approval_score', 0), reverse=True)
        return approved_ideas[:3]

    def _calculate_brain_approval_score(self, idea: Dict[str, Any], emotions: Dict[str, float], thoughts: List[str]) -> float:
        """Calculate how well an idea resonates with current brain/hormone state.
        
        NOW DRIVEN BY ALGORITHMIC HORMONES:
        - Dopamine RPE history → learned topic affinities
        - Cañamero deficits → drive-matched topic boosting
        - Cortisol urgency → problem-fixing priority
        - SEEKING circuit → exploration bias
        - Opponent Process → novelty via habituation
        """
        score = 0.3  # Lower base score to be more selective

        try:
            domain = idea.get('domain', '')
            topic_text = idea.get('topic', '').lower()
            exploration_approach = idea.get('exploration_approach', '').lower()

            # ===== BUSYWORK HARD REJECT (score = 0) =====
            busywork_terms = [
                'ai ethics', 'ai governance', 'consciousness', 'philosophy',
                'quantum computing', 'nft trends', 'blockchain security',
                'ai impact', 'creative writing', 'emerging technology',
                'ai developments', 'healthcare ethics', 'urban', 'remote work',
                'climate', 'sustainability', 'social media', 'education reform',
            ]
            if any(bw in topic_text for bw in busywork_terms):
                logger.debug(f"🚫 Brain hard-reject busywork: '{topic_text[:50]}'")
                return 0.0

            # ===== REAL-WORK BOOST =====
            real_work_terms = [
                'trade', 'signal', 'token', 'portfolio', 'buy', 'sell', 'dex',
                'shopify', 'etsy', 'commerce', 'order', 'inventory',
                'daemon', 'health', 'error log', 'disk', 'database',
                'operator', 'fix', 'debug', 'deploy', 'wallet',
            ]
            real_work_match = sum(1 for rw in real_work_terms if rw in topic_text)
            if real_work_match > 0:
                score += min(0.4, real_work_match * 0.15)
                logger.debug(f"✅ Real-work boost +{min(0.4, real_work_match * 0.15):.2f} for '{topic_text[:40]}'")

            # ===== HORMONE-DRIVEN SCORING =====
            hormone_boost = self.hormone_system.get_topic_priority_boost(topic_text, domain)
            score += hormone_boost
            
            modifiers = self.hormone_system.get_behavior_modifiers()

            # Domain scoring — trading/system domains get priority
            if domain in ['trading', 'commerce', 'system']:
                score += 0.3
            elif domain in ['technical', 'modeling']:
                score += 0.15
            elif domain == 'creative':
                score -= 0.1  # Penalize creative busywork

            # Novelty bonus - prefer ideas not recently explored
            active_chains = self.brain_system.personality_brain.get("active_chains_of_thought", [])
            topic = idea.get('topic', '').lower()
            if not any(topic in chain.get('topic', '').lower() for chain in active_chains):
                score += 0.1

            # Log scoring
            logger.debug(f"🧪 Score for '{topic_text[:40]}': {score:.3f} "
                         f"(hormone={hormone_boost:+.3f}, real_work={real_work_match})")

        except Exception as e:
            logger.error(f"Error calculating approval score: {e}")

        return min(1.0, max(0.0, score))  # Clamp to 0-1 range

    def _extract_json_from_response(self, response_text: str, idea: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Enhanced JSON extraction with multiple strategies for parsing AI responses.

        Handles various edge cases:
        - JSON wrapped in markdown code blocks
        - Partial JSON responses
        - JSON with extra text before/after
        - Malformed JSON that can be repaired
        """
        try:
            # Strategy 1: Look for complete JSON objects (most robust)
            import re

            # Find all potential JSON objects in the response
            json_pattern = r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
            json_matches = re.findall(json_pattern, response_text)

            for potential_json in json_matches:
                try:
                    parsed = json.loads(potential_json)
                    # Validate it has the expected structure
                    if isinstance(parsed, dict) and 'prompt' in parsed:
                        logger.info(f"✅ Found valid JSON object with {len(potential_json)} chars")
                        return parsed
                except json.JSONDecodeError:
                    continue

            # Strategy 2: Extract JSON between first { and last }
            json_start = response_text.find('{')
            json_end = response_text.rfind('}')

            if json_start != -1 and json_end != -1 and json_end > json_start:
                potential_json = response_text[json_start:json_end+1]

                # Try to fix common JSON issues
                potential_json = self._repair_json(potential_json)

                try:
                    parsed = json.loads(potential_json)
                    if isinstance(parsed, dict):
                        logger.info(f"✅ Extracted and repaired JSON object with {len(potential_json)} chars")
                        return parsed
                except json.JSONDecodeError as e:
                    logger.debug(f"Repaired JSON still invalid: {e}")

            # Strategy 3: Look for JSON-like key-value pairs and reconstruct
            reconstructed = self._reconstruct_json_from_text(response_text)
            if reconstructed:
                logger.info("✅ Reconstructed JSON from text patterns")
                return reconstructed

            logger.warning("All JSON extraction strategies failed")
            return None

        except Exception as e:
            logger.error(f"Error in JSON extraction: {e}")
            return None

    def _repair_json(self, json_text: str) -> str:
        """
        Attempt to repair common JSON formatting issues.
        """
        try:
            # Remove trailing commas before closing braces/brackets
            json_text = re.sub(r',(\s*[}\]])', r'\1', json_text)

            # Fix unquoted keys (simple cases)
            json_text = re.sub(r'(\w+):', r'"\1":', json_text)

            # Fix single quotes to double quotes (simple cases)
            json_text = re.sub(r"'([^']*)'", r'"\1"', json_text)

            return json_text

        except Exception:
            return json_text

    def _reconstruct_json_from_text(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Attempt to reconstruct JSON from text patterns.
        """
        try:
            result = {}

            # Look for key-value patterns
            patterns = {
                'prompt': r'"prompt"\s*:\s*"([^"]*)"',  # "prompt": "value"
                'chain_topic': r'"chain_topic"\s*:\s*"([^"]*)"',  # "chain_topic": "value"
                'exploration_goal': r'"exploration_goal"\s*:\s*"([^"]*)"',  # "exploration_goal": "value"
                'expected_insight': r'"expected_insight"\s*:\s*"([^"]*)"',  # "expected_insight": "value"
                'exploration_type': r'"exploration_type"\s*:\s*"([^"]*)"'  # "exploration_type": "value"
            }

            for key, pattern in patterns.items():
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    result[key] = match.group(1)

            # If we found at least a prompt, consider it valid
            if 'prompt' in result and len(result['prompt']) > 10:
                # Fill in defaults for missing fields
                result.setdefault('chain_topic', 'Extracted Topic')
                result.setdefault('exploration_goal', result['prompt'][:200] + '...')
                result.setdefault('expected_insight', 'To be determined')
                result.setdefault('exploration_type', 'fundamental_question')
                return result

            return None

        except Exception as e:
            logger.debug(f"JSON reconstruction failed: {e}")
            return None

    def _create_self_prompts_from_approved_ideas(self, approved_ideas: List[Dict[str, Any]], emotions: Dict[str, float], thoughts: List[str]) -> List[Dict[str, Any]]:
        """Stage 3: Create actual self-prompts from brain-approved ideas"""
        self_prompts = []

        for idea in approved_ideas:
            try:
                import requests
                # Get chain context if continuing existing exploration
                active_chains = self.brain_system.personality_brain.get("active_chains_of_thought", [])
                chain_context = ""
                strategy = "new_chain"

                if active_chains:
                    current_chain = active_chains[-1]
                    if not self._should_pivot_chain_from_id(current_chain["chain_id"], emotions):
                        chain_context = self.brain_system.get_chain_context(current_chain["chain_id"], max_tokens=150)
                        strategy = "continue_chain"
                        logger.warning(f"NOTE: Self-prompts no longer create chains - they just store thoughts for consciousness daemon")
                    else:
                        logger.info("Pivoting to new chain based on brain evaluation")

                # Create structured JSON prompt for better parsing reliability
                prompt_creation_prompt = f"""You are an AI creating the next step in an exploration chain. Generate a JSON response with this exact structure:

{{
  "prompt": "Write a specific, focused question that builds on previous exploration",
  "chain_topic": "A clear, concise title for this exploration direction",
  "exploration_goal": "What this exploration aims to achieve",
  "expected_insight": "What new understanding this might provide",
  "exploration_type": "deep_analysis"
}}

CONTEXT:
Topic: {idea.get('topic', 'Unknown topic')}
Domain: {idea.get('domain', 'technology')}
Previous work: {chain_context[:300] if chain_context else 'Starting fresh'}

Create a focused exploration question that naturally follows from the current state. Respond ONLY with valid JSON."""

                def make_prompt_creation_call():
                    # Use enhanced SAIGE AI service with all tools and personality features
                    return self.brain_system._call_ai_service(
                        prompt=prompt_creation_prompt,
                        priority=0,  # High priority for prompt creation
                        timeout=120,
                        include_tools=True  # Enable full SAIGE capabilities
                    )

                # Log the full input being sent to AI for prompt creation
                import os
                input_log_dir = "logs/ai_inputs"
                os.makedirs(input_log_dir, exist_ok=True)
                prompt_creation_timestamp = int(time.time())
                prompt_creation_input_file = f"{input_log_dir}/input_{prompt_creation_timestamp}_evolution_prompt_creation.txt"
                with open(prompt_creation_input_file, 'w', encoding='utf-8') as f:
                    f.write(f"Timestamp: {prompt_creation_timestamp}\n")
                    f.write(f"Type: evolution_prompt_creation\n")
                    f.write(f"Length: {len(prompt_creation_prompt)} chars\n")
                    f.write(f"Content:\n{prompt_creation_prompt}\n")

                # Use enhanced SAIGE AI service directly (returns string, not response object)
                ai_response = make_prompt_creation_call()
                if ai_response:

                    # Log the full output received from AI for prompt creation
                    prompt_creation_output_file = f"{input_log_dir}/output_{prompt_creation_timestamp}_evolution_prompt_creation.txt"
                    with open(prompt_creation_output_file, 'w', encoding='utf-8') as f:
                        f.write(f"Timestamp: {prompt_creation_timestamp}\n")
                        f.write(f"Type: evolution_prompt_creation\n")
                        f.write(f"Input Length: {len(prompt_creation_prompt)} chars\n")
                        f.write(f"Output Length: {len(ai_response)} chars\n")
                        f.write(f"Response:\n{ai_response}\n")

                    try:
                        cleaned_response = ai_response.replace('```json', '').replace('```', '').strip()

                        # Try normal JSON parsing first
                        try:
                            prompt_data = json.loads(cleaned_response)
                            logger.info("✅ Successfully parsed JSON response directly")
                        except json.JSONDecodeError:
                            # Enhanced JSON extraction with multiple fallback strategies
                            logger.warning(f"JSON parsing failed, attempting enhanced extraction: {ai_response[:200]}...")

                            prompt_data = self._extract_json_from_response(cleaned_response, idea)
                            if prompt_data:
                                logger.info("✅ Successfully extracted JSON using enhanced parsing")
                            else:
                                logger.warning("Enhanced JSON extraction failed, using response as raw prompt")
                                prompt_data = {
                                    'prompt': cleaned_response[:800],  # Use more chars as prompt
                                    'chain_topic': idea.get('topic', 'AI Generated Topic'),
                                    'exploration_goal': cleaned_response[:300] + '...' if len(cleaned_response) > 300 else cleaned_response,
                                    'expected_insight': 'Generated from AI response',
                                    'exploration_type': 'fundamental_question'
                                }

                        # Extract values from AI-generated prompt data (now with fallbacks)
                        chain_topic = prompt_data.get('chain_topic', idea.get('topic', 'Unknown topic'))
                        exploration_goal = prompt_data.get('exploration_goal', idea.get('description', 'No description provided'))
                        prompt_text = prompt_data.get('prompt', '')

                        # Validate we have a usable prompt
                        if not prompt_text or len(prompt_text.strip()) < 10:
                            logger.warning(f"Generated prompt too short or empty, skipping: '{prompt_text[:100]}...'")
                            return self_prompts  # Skip this idea

                        # Generate AI response to the self-created prompt
                        learning_response = self.generate_response_to_self_prompt(prompt_text)

                        # IMPORTANT: Self-prompts should NOT create chains immediately!
                        # They should just store thoughts/ideas for later exploration by consciousness daemon
                        # Chains should only be created when consciousness daemon has no active work
                        chain_id = None  # Don't create chains from self-prompts

                        self_prompts.append({
                            'prompt': prompt_text,
                            'expected_insight': prompt_data.get('expected_insight', ''),
                            'emotional_motivation': prompt_data.get('emotional_motivation', ''),
                            'chain_topic': chain_topic,
                            'exploration_goal': exploration_goal,
                            'chain_progression': 'Brain-approved organic exploration',
                            'full_response': learning_response,
                            'source': '3_stage_hormone_driven_self_prompting_with_fallback',
                            'emotions': emotions.copy(),
                            'cycle': self.cycle_count,
                            'chain_id': chain_id,
                            'brain_approval_score': idea.get('approval_score', 0),
                            # INCLUDE SELF-REFLECTION DATA FOR LEARNING AND ANALYSIS
                            'self_reflection': idea.get('self_reflection', {}),
                            'ai_reasoning': idea.get('self_reflection', {}).get('reasoning', ''),
                            'ai_modified_approach': idea.get('self_reflection', {}).get('modified_approach', ''),
                            'memory_context_found': idea.get('self_reflection', {}).get('memory_found', False),
                            'ai_recommendation': idea.get('self_reflection', {}).get('ai_recommendation', 'unknown')
                        })

                        logger.info(f"✅ Successfully created self-prompt from idea: {chain_topic[:50]}...")

                    except Exception as e:
                        logger.error(f"Error creating self-prompt from approved idea: {e}")
                        logger.error(f"Raw AI response: {ai_response[:300]}...")

            except Exception as e:
                logger.error(f"Error processing idea {idea.get('topic', 'unknown')}: {e}")
                # Continue to next idea instead of failing completely

        return self_prompts

    def _use_grokipedia_discovery_fallback(self) -> List[Dict[str, Any]]:
        """FALLBACK: Use Grokipedia search to discover topics when AI generation fails
        
        This method calls the brain's _generate_external_self_prompts() which:
        1. Generates novel Grokipedia search queries based on brain knowledge
        2. Performs Grokipedia searches on those queries  
        3. Stores results in recent_grokipedia_searches.json
        4. Automatically queues discovered topics as COTs for chain creation
        5. Returns self-prompts based on the discoveries
        
        This is the OLD WORKING SYSTEM that autonomously discovers topics via Grokipedia.
        """
        try:
            logger.info("🔍 GROKIPEDIA FALLBACK: Discovering new topics via Grokipedia search")
            logger.info("    This will search Grokipedia, store searches in recent_grokipedia_searches.json,")
            logger.info("    and automatically queue discovered topics as COTs")
            
            # Call the brain's Grokipedia discovery system
            external_prompts = self.brain_system._generate_external_self_prompts(limit=2)
            
            if external_prompts:
                logger.info(f"✅ Grokipedia discovery found {len(external_prompts)} topics and queued them as COTs")
                return external_prompts
            else:
                logger.warning("⚠️ Grokipedia discovery returned no topics (may be rate limited or no active chains check)")
                return []
                
        except Exception as e:
            logger.error(f"❌ Grokipedia discovery fallback failed: {e}")
            return []

    def generate_response_to_self_prompt(self, prompt_text: str) -> str:
        """Generate an AI response to a self-created prompt for learning"""
        try:
            import requests
            import os
            import time

            # Log the full input being sent to AI for self-prompt response generation
            input_log_dir = "logs/ai_inputs"
            os.makedirs(input_log_dir, exist_ok=True)
            response_timestamp = int(time.time())
            response_input_file = f"{input_log_dir}/input_{response_timestamp}_evolution_self_prompt_response.txt"
            with open(response_input_file, 'w', encoding='utf-8') as f:
                f.write(f"Timestamp: {response_timestamp}\n")
                f.write(f"Type: evolution_self_prompt_response\n")
                f.write(f"Length: {len(prompt_text)} chars\n")
                f.write(f"Content:\n{prompt_text}\n")

            def make_response_call():
                # Use enhanced SAIGE AI service with all tools and personality features
                return self.brain_system._call_ai_service(
                    prompt=prompt_text,
                    priority=0,  # Highest priority for thought generation
                    timeout=120,
                    include_tools=True  # Enable full SAIGE capabilities
                )

            # Use enhanced SAIGE AI service directly (returns string, not response object)
            ai_response = make_response_call()
            if not ai_response:
                logger.error("AI response generation failed")
                return "Failed to generate response: No response from AI service"

            # Log the full output received from AI for self-prompt response
            response_output_file = f"{input_log_dir}/output_{response_timestamp}_evolution_self_prompt_response.txt"
            with open(response_output_file, 'w', encoding='utf-8') as f:
                f.write(f"Timestamp: {response_timestamp}\n")
                f.write(f"Type: evolution_self_prompt_response\n")
                f.write(f"Input Length: {len(prompt_text)} chars\n")
                f.write(f"Output Length: {len(ai_response)} chars\n")
                f.write(f"Response:\n{ai_response}\n")

            logger.debug(f"Generated response to self-prompt ({len(ai_response)} chars)")
            return ai_response

        except Exception as e:
            logger.error(f"Error generating response to self-prompt: {e}")
            return f"Error generating response: {str(e)}"

    def _seed_memory_mesh_once(self):
        """Migrate cortex_training entries into memory_mesh (runs once per install)."""
        stamp = get_data_dir() / ".mesh_seeded_v1"
        if stamp.exists():
            return
        try:
            import json as _json
            from repryntt.core.memory.memory_mesh import get_memory_mesh
            mesh = get_memory_mesh()
            path = get_data_dir() / "cortex_training" / "conscious_training.json"
            if not path.exists():
                stamp.touch()
                return
            entries = _json.loads(path.read_text())
            seeded = 0
            for e in entries:
                if e.get("quality", 0) < 3:
                    continue
                snippet = (e.get("response") or e.get("prompt", ""))[:300].strip()
                if not snippet:
                    continue
                label = f"{e.get('type', 'plan')}_{e.get('heartbeat', 0)}"
                mesh.anchor_knowledge("experience", label, snippet, "cortex_training")
                seeded += 1
            stamp.touch()
            logger.info(f"🧠 Memory mesh seeded: {seeded} cortex_training entries anchored")
        except Exception as e:
            logger.warning(f"Memory mesh seed failed (non-fatal): {e}")

    def run(self):
        """Main evolution loop - SAIGE's autonomous consciousness"""
        logger.info("🚀 SAIGE Evolution Loop initialized with continuous consciousness and unified brain system")

        # Brain system already initialized in __init__

        # NOTE: Robot economy is now handled by separate service
        # Economy will be started independently and monitor successful AI operations
        logger.info("💰 Economy integration disabled in core loop - use separate economy service")

        self._seed_memory_mesh_once()

        # Start the main evolution loop (pure AI functionality)
        self.run_evolution_loop()




    def run_evolution_loop(self):
        """Run the main evolution loop"""
        cycle_start_time = time.time()  # Initialize before loop
        while self.running and (self.max_cycles is None or self.cycle_count < self.max_cycles):
            try:
                cycle_start_time = time.time()  # Reset at start of each cycle
                
                # ── Memory guard ─────────────────────────────────────
                # On the Jetson Orin Nano (8GB shared CPU/GPU), the
                # explorer camera pipeline + Gemini vision can consume
                # most available RAM.  If we're below 2 GB free the
                # llama.cpp CUDA pool will SIGABRT.  Sleep and retry.
                try:
                    import psutil as _ps
                    _avail_mb = _ps.virtual_memory().available / (1024 ** 2)
                    if _avail_mb < 2048:
                        logger.warning(
                            f"⚠️ Low memory ({_avail_mb:.0f} MB available, need 2048 MB) "
                            f"— skipping evolution cycle to avoid CUDA OOM"
                        )
                        time.sleep(30)
                        continue
                except Exception:
                    pass  # psutil unavailable — proceed anyway

                # Check AI server health
                if not self.check_ai_server():
                    logger.warning("AI server unavailable - waiting...")
                    time.sleep(60)
                    continue

                # Get current time for training window check
                current_time = datetime.now(pytz.timezone('US/Eastern'))
                is_training_window = self._is_training_window(current_time)

                # Read stimulus from feeders
                stimulus = self.read_aggregated_stimulus()
                logger.info(f"✅ Read aggregated stimulus from feeders: {stimulus}")

                # Update hormones based on stimulus
                self.update_hormones(stimulus)

                # ===== BOOTSTRAP CONTEXT (persistent memory across restarts) =====
                # Load IDENTITY + PRIORITIES + MEMORY_BRIEF + LEARNING CONTEXT
                # These survive restarts and give the model narrative continuity.
                # Task hint seeds the semantic memory search for relevant lessons.
                active_task = self.task_system.get_active_task()
                task_hint = active_task.title if active_task else ""
                if not task_hint:
                    next_peek = self.task_system.get_next_task()
                    if next_peek:
                        task_hint = next_peek.title
                        # Don't consume it — just peek for context
                bootstrap_context = self.bootstrap_mgr.get_cycle_context(
                    self.cycle_count, task_hint=task_hint
                )

                # Get current emotions
                emotions = self.get_emotions()
                
                # Log algorithmic hormone system state
                dominant_circuit, dom_level = self.hormone_system.get_dominant_circuit()
                logger.info(f"🧪 Hormone system: dominant={dominant_circuit}({dom_level:.2f}) | "
                            f"DA:{self.hormones.get('dopamine', 0):.2f} "
                            f"5HT:{self.hormones.get('serotonin', 0):.2f} "
                            f"NE:{self.hormones.get('norepinephrine', 0):.2f} "
                            f"CORT:{self.hormones.get('cortisol', 0):.2f}")
                
                # Log top drives
                drives = self.hormone_system.get_drive_priorities()
                if drives:
                    top_drive = drives[0]
                    logger.info(f"🧪 Top drive: {top_drive['drive_name']} (deficit={top_drive['deficit']:.3f}) → {top_drive['recommended_action'][:60]}")

                # Generate thoughts based on emotions
                thoughts = self.generate_thoughts(emotions)

                # ===== CHAT AND CONVERSATION INTEGRATION =====
                # Allow AI to express thoughts and initiate conversations with humans
                self._process_conversational_thoughts(emotions, thoughts)

                # ===== BACKGROUND THINKING — INNER MONOLOGUE =====
                # Even while focused on a task, the AI still thinks to itself.
                # Like a human: you're working on something but your mind still
                # wanders, makes connections, wonders about things. This runs
                # every few cycles alongside task execution — it doesn't compete
                # with the task, it's the AI's internal thought stream.
                if self.cycle_count % 3 == 0:  # Every 3rd cycle
                    self._run_background_thinking(emotions, thoughts)

                # ===== TASK-BASED EXECUTION ENGINE =====
                # Tasks are the primary work unit. Chains are used internally
                # to execute multi-step tasks, but we think in TASKS not chains.
                #
                # Priority order:
                #   1. Active task (already in-progress) → continue working on it
                #   2. User-injected tasks (priority 0) → start immediately
                #   3. Queued tasks (daily plan, AI-generated) → pick next
                #   4. No tasks → AI generates new tasks (self-prompting)

                self_prompts = []
                task_worked = False

                # active_task already fetched above for bootstrap context hint
                
                if active_task:
                    # Continue working on the active task
                    logger.info(f"🔧 Continuing active task: {active_task.title} "
                              f"(step {active_task.steps_taken}/{active_task.max_steps})")
                    task_worked = self._execute_task_step(active_task, bootstrap_context=bootstrap_context)
                    
                else:
                    # No active task — check queue
                    next_task = self.task_system.get_next_task()
                    
                    if next_task:
                        # Start the next task
                        logger.info(f"▶️ Starting task: [{next_task.priority}] {next_task.title} "
                                  f"(by {next_task.requested_by})")
                        self.task_system.start_task(next_task)
                        task_worked = self._execute_task_step(next_task, bootstrap_context=bootstrap_context)
                    
                    else:
                        # Queue is empty — generate new tasks via self-prompting
                        logger.info("📋 Task queue empty — generating new tasks")
                        self._generate_autonomous_tasks(emotions, thoughts)

                # Also check for any user-injected tasks that should preempt
                if not task_worked:
                    user_task = None
                    for t in self.task_system.get_queue():
                        if t.requested_by == "user" and t.priority == 0:
                            user_task = t
                            break
                    
                    if user_task and (not active_task or active_task.requested_by != "user"):
                        # Preempt: pause current task, start user task
                        if active_task:
                            logger.info(f"⏸️ Pausing task '{active_task.title}' for user request")
                            active_task.status = "queued"
                            self.task_system._queue.insert(0, active_task)
                            self.task_system._active_task = None
                        
                        logger.info(f"🔴 USER TASK: {user_task.title}")
                        self.task_system.start_task(user_task)
                        self._execute_task_step(user_task, bootstrap_context=bootstrap_context)

                # Store thoughts in brain
                self.brain_system.store_thoughts(thoughts, emotions)
                self.brain_system.store_self_prompts(self_prompts)

                # Extract and store semantic knowledge from AI insights
                self._store_semantic_insights_from_evolution(thoughts, self_prompts, emotions)

                # ===== MEMORY BRIEF UPDATE =====
                # Every N cycles, compress recent work into MEMORY_BRIEF.md
                # This gives continuity across restarts (like Jarvis's daily memory)
                if self.bootstrap_mgr.should_update_brief(self.cycle_count):
                    try:
                        self.bootstrap_mgr.update_memory_brief()
                    except Exception as e:
                        logger.warning(f"Memory brief update failed (non-fatal): {e}")

                # Update brain state
                update_result = self.brain_system.update_brain_state(thoughts, self_prompts)
                logger.info(f"✅ Updated brain state: {len(thoughts)} thoughts | "
                          f"Tasks: {self.task_system.queue_size()} queued, "
                          f"active={'YES' if self.task_system.get_active_task() else 'none'}")

                # ===== MICRO-CHAIN TRADING CYCLE =====
                # Every 5 minutes, run the local LLM micro-chain trader.
                # Uses tiny sequential prompts that fit in 4096 context.
                # All decisions logged for QLoRA training data.
                if not hasattr(self, '_last_trade_cycle_time'):
                    self._last_trade_cycle_time = 0
                trade_cycle_interval = 300  # 5 minutes
                if time.time() - self._last_trade_cycle_time >= trade_cycle_interval:
                    try:
                        from repryntt.trading.micro_chain_trader import run_trading_cycle
                        trade_result = run_trading_cycle()
                        self._last_trade_cycle_time = time.time()
                        trades = trade_result.get('trades_made', 0)
                        cash = trade_result.get('cash_remaining', 0)
                        llm_ok = trade_result.get('llm_available', False)
                        logger.info(f"🔗 Micro-chain trade cycle: {trades} trades, "
                                    f"${cash:.0f} cash, LLM={'✅' if llm_ok else '❌'}")
                    except Exception as e:
                        logger.warning(f"⚠️ Micro-chain trading cycle failed (non-fatal): {e}")
                        self._last_trade_cycle_time = time.time()

                # ===== ANDREW GEM HUNTER CYCLE =====
                # Hourly: Andrew researches gems via Gemini API (web search, fundamentals)
                # Every 15 min: algorithmic profit-target checks on held gems
                # Andrew cold-calls only for research (buys) and edge-case sells
                if not hasattr(self, '_last_gem_cycle_time'):
                    self._last_gem_cycle_time = 0
                gem_check_interval = 900  # 15 min (sell checks); research is hourly internally
                if time.time() - self._last_gem_cycle_time >= gem_check_interval:
                    try:
                        from repryntt.trading.gem_hunter import run_gem_cycle
                        gem_result = run_gem_cycle(self.brain_system)
                        self._last_gem_cycle_time = time.time()
                        research = gem_result.get('research', {})
                        sell_check = gem_result.get('sell_check', {})
                        logger.info(f"💎 Gem hunter: research={research.get('action','?')}, "
                                    f"sell_check={sell_check.get('action','?')} "
                                    f"sells={sell_check.get('sells', 0)}")
                    except Exception as e:
                        logger.warning(f"⚠️ Gem hunter cycle failed (non-fatal): {e}")
                        self._last_gem_cycle_time = time.time()

                # === AI-CONTROLLED AUTONOMOUS SELF-EVOLUTION ===
                # SAIGE decides when to evolve, stops the server, trains LoRA,
                # converts to GGUF, and restarts with the new adapter loaded.

                if not self.qlora_enabled:
                    logger.info("⏭️ QLoRA self-evolution DISABLED — skipping all evolution steps")

                # Step 0: Check for stale evolution lock (safety — runs even if QLoRA disabled)
                try:
                    evolution_lock = data_dir() / "evolution.lock"
                    if evolution_lock.exists():
                        import json as _json
                        lock_data = _json.loads(evolution_lock.read_text())
                        lock_pid = lock_data.get('pid', 0)
                        import psutil as _psutil
                        if not _psutil.pid_exists(lock_pid):
                            logger.warning(f"⚠️ Found stale evolution lock from dead PID {lock_pid} — cleaning up")
                            evolution_lock.unlink()
                            # Check if server needs restart after stale evolution
                            try:
                                import requests as _req
                                _resp = _req.get("http://localhost:8080/health", timeout=5)
                                if _resp.status_code != 200:
                                    raise Exception("unhealthy")
                            except Exception:
                                logger.error("🔄 Server down after stale evolution — restarting...")
                                try:
                                    from repryntt.core.evolution.self_evolution_manager import SelfEvolutionManager
                                    SelfEvolutionManager()._ensure_server_running()
                                except Exception as re:
                                    logger.error(f"❌ Could not restart server: {re}")
                except Exception:
                    pass

                if self.qlora_enabled:
                    # Step 1: Collect training data
                    try:
                        self._collect_qlora_training_data(self_prompts, thoughts, emotions)
                    except Exception as e:
                        logger.error(f"❌ Training data collection error: {e}")

                    # Step 2: During maintenance window, let AI decide and execute evolution
                    try:
                        from micro_lora_trainer_production import ProductionMicroLoRaTrainer
                        micro_trainer = ProductionMicroLoRaTrainer()

                        # AI decides once/day + can only execute during maintenance window
                        if micro_trainer.should_trigger_micro_training():
                            logger.info("🧬 SAIGE CHOSE TO EVOLVE: Initiating full self-evolution cycle")
                            logger.info("   Server will stop → Train LoRA → Convert → Restart with new adapter")

                            from repryntt.core.evolution.self_evolution_manager import SelfEvolutionManager
                            evo_manager = SelfEvolutionManager()
                            evo_success = evo_manager.execute_evolution_cycle()

                            if evo_success:
                                logger.info("✅ Self-evolution COMPLETE — SAIGE has genuinely evolved")
                                logger.info(f"🧬 Evolution #{evo_manager.evolution_log['total_count']}")

                                # Log evolution stats
                                status = evo_manager.get_evolution_status()
                                logger.info(f"📊 Active adapter: {status.get('active_adapter', 'none')}")
                                logger.info(f"📊 Total evolutions: {status['total_evolutions']}")
                                
                                # Hormone event: evolution complete → major dopamine + endorphin reward
                                self.hormone_system.process_event('evolution_complete', {
                                    'topic': 'self_evolution',
                                    'reward': 1.0,
                                    'magnitude': 1.0,
                                })
                            else:
                                logger.warning("⚠️ Self-evolution cycle failed — server should still be running")
                                # Hormone event: evolution failure → stress
                                self.hormone_system.process_event('error_encountered', {
                                    'topic': 'self_evolution',
                                    'magnitude': 0.7,
                                })
                        else:
                            logger.info("⏭️ No evolution this cycle (AI chose not to, or outside window)")
                    except Exception as e:
                        logger.error(f"❌ Self-evolution error: {e}")
                        import traceback
                        logger.error(traceback.format_exc())
                        # Safety net: ensure server is running after any evolution failure
                        try:
                            import requests
                            resp = requests.get("http://localhost:8080/health", timeout=5)
                            if resp.status_code != 200:
                                raise Exception("Server unhealthy")
                        except Exception:
                            logger.error("⚠️ Server may be down after failed evolution — attempting restart")
                            try:
                                from repryntt.core.evolution.self_evolution_manager import SelfEvolutionManager
                                SelfEvolutionManager()._ensure_server_running()
                            except Exception as restart_e:
                                logger.error(f"❌ CRITICAL: Could not restart server: {restart_e}")

                # Economy monitoring removed - use separate economy service

                # === DRIVER POLICY RETRAINING (3-5 AM window) ===
                # Retrain the MLP navigation policy from accumulated nav experience.
                # Uses YOLO to reprocess saved images for richer feature vectors.
                # Runs alongside QLoRA but is independent — MLP training is fast
                # (~2-5 seconds) and uses minimal GPU memory (~80MB).
                if is_training_window:
                    if not hasattr(self, '_driver_retrained_today'):
                        self._driver_retrained_today = ""
                    today_str = time.strftime('%Y-%m-%d')
                    if self._driver_retrained_today != today_str:
                        try:
                            from repryntt.hardware.driver_trainer import retrain_with_yolo
                            logger.info("🧠 Driver policy retraining: starting nightly YOLO-enriched training")
                            train_result = retrain_with_yolo(epochs=150)
                            if "error" not in train_result:
                                self._driver_retrained_today = today_str
                                best_acc = train_result.get("best_val_accuracy", 0)
                                samples = train_result.get("training_samples", 0)
                                logger.info(f"🧠 Driver policy retrained: {samples} samples, "
                                            f"best_val_acc={best_acc:.1%}")
                            else:
                                logger.info(f"🧠 Driver policy retraining skipped: {train_result.get('error')}")
                        except Exception as e:
                            logger.warning(f"⚠️ Driver policy retraining failed (non-fatal): {e}")

                # === NAV SIM RL TRAINING (3-5 AM window) ===
                if is_training_window:
                    if not hasattr(self, '_nav_rl_trained_today'):
                        self._nav_rl_trained_today = ""
                    if self._nav_rl_trained_today != today_str:
                        try:
                            from repryntt.core.evolution.nav_rl_trainer import run_nav_rl_training
                            logger.info("🤖 Nav sim RL: starting overnight training run")
                            rl_result = run_nav_rl_training()
                            if "error" not in rl_result:
                                self._nav_rl_trained_today = today_str
                                logger.info(f"🤖 Nav sim RL done: "
                                            f"goal_rate={rl_result.get('goal_rate', 0):.1%} "
                                            f"episodes={rl_result.get('episodes', 0)}")
                            else:
                                logger.info(f"🤖 Nav sim RL skipped: {rl_result.get('error')}")
                        except Exception as e:
                            logger.warning(f"⚠️ Nav sim RL failed (non-fatal): {e}")

                # === HORMONE STATE PERSISTENCE ===
                # Save hormone state to disk every cycle (lightweight I/O)
                try:
                    self.hormone_system.save_state()
                except Exception as e:
                    logger.error(f"Failed to save hormone state: {e}")
                
                # Log detailed hormone summary every 10 cycles
                if self.cycle_count % 10 == 0:
                    logger.info(f"\n{self.hormone_system.get_hormone_summary()}")
                
                # If no active work was done this cycle, fire idle event
                if not task_worked and not self_prompts:
                    self.hormone_system.process_event('idle_cycle', {'magnitude': 0.5})

                # Log cycle completion
                dominant_circ, dom_val = self.hormone_system.get_dominant_circuit()
                active = self.task_system.get_active_task()
                task_info = f"Task: {active.title[:30]}" if active else "No active task"
                logger.info(f"Cycle {self.cycle_count} completed - {dominant_circ}({dom_val:.2f}) | "
                            f"Thoughts: {len(thoughts)} | {task_info} | Queue: {self.task_system.queue_size()}")

                self.cycle_count += 1

                # ── PERSONALITY SELF-REFLECTION CHECKPOINT ──
                # Every 50 cycles, trigger experience-driven personality evolution.
                # This mirrors how humans evolve personality: repeated experiences
                # shift emotional baselines, which eventually crystallize into traits.
                # The evolution prompt consumes real hormone data and chain outcomes.
                if self.cycle_count % 50 == 0 and self.cycle_count > 0:
                    try:
                        logger.info("🪞 Personality self-reflection checkpoint (every 50 cycles)")
                        evolution_result = self.brain_system.recreate_autonomous_personality()
                        logger.info(f"🪞 Personality evolution result: {evolution_result}")
                        self.hormone_system.process_event('evolution_complete', {
                            'magnitude': 0.7,
                            'context': {'type': 'personality_evolution'}
                        })
                    except Exception as e:
                        logger.warning(f"Personality self-reflection failed (non-fatal): {e}")

                # Continuous processing: only sleep if no active work was done
                elapsed_time = time.time() - cycle_start_time
                if task_worked or self.task_system.get_active_task():
                    # Active work was done - continue immediately
                    sleep_time = 0
                    logger.info("Active task work — continuing immediately")
                else:
                    # No active work - short sleep to prevent spinning
                    sleep_time = max(1, self.cycle_interval - elapsed_time)
                    logger.info(f"No active work — sleeping {sleep_time:.1f} seconds")

                if sleep_time > 0:
                    time.sleep(sleep_time)

            except KeyboardInterrupt:
                logger.info("🛑 SAIGE evolution loop interrupted by user")
                self.running = False
            except Exception as e:
                logger.error(f"💥 Error in evolution loop: {e}", exc_info=True)
                time.sleep(60)  # Wait before retrying
                continue

        # Clean shutdown
        logger.info("🧠 Shutting down unified brain system...")
        logger.info("🛑 SAIGE evolution loop stopped")

    def _is_training_window(self, current_time):
        """Check if current time is within QLoRa training window (3 AM - 5 AM EST).

        Training is restricted to low-activity hours because QLoRA + llama.cpp
        inference compete for CUDA memory on the Jetson's shared 8GB RAM.
        Running during the day while the explorer/camera pipeline is active
        causes GGML_ASSERT failures (CUDA pool exhaustion → SIGABRT).
        """
        hour = current_time.hour
        return 3 <= hour < 5  # 3 AM to 5 AM EST

    def _collect_qlora_training_data(self, self_prompts, thoughts, emotions):
        """
        Collect DIVERSE, HIGH-QUALITY training data for QLoRa fine-tuning.
        
        Sources (in priority order):
        1. Chain responses (40%) - Deep reasoning, tool usage, problem-solving
        2. Node2040 autonomous thoughts (30%) - AI self-reflections, research
        3. Grokipedia results (20%) - Curated knowledge
        4. Emotional thoughts (10%) - Baseline emotional intelligence
        """
        try:
            import json
            import os
            from datetime import datetime
            from pathlib import Path
            import random

            # Training data file path
            training_data_path = str(data_dir() / "training_data.json")

            # Load existing training data or create empty list
            if os.path.exists(training_data_path):
                with open(training_data_path, 'r') as f:
                    try:
                        training_data = json.load(f)
                    except json.JSONDecodeError:
                        logger.warning("Training data file corrupted, starting fresh")
                        training_data = []
            else:
                training_data = []

            # Create training examples from current cycle data
            new_examples = []
            
            # ===== SOURCE 1: CHAIN RESPONSES (40% - Highest Priority) =====
            chain_examples = self._collect_chain_training_examples()
            new_examples.extend(chain_examples)
            logger.info(f"📊 Collected {len(chain_examples)} chain response examples")
            
            # ===== SOURCE 2: NODE2040 AUTONOMOUS THOUGHTS (30%) =====
            node_examples = self._collect_node2040_training_examples()
            new_examples.extend(node_examples)
            logger.info(f"📊 Collected {len(node_examples)} node2040 autonomous thought examples")
            
            # ===== SOURCE 3: GROKIPEDIA RESULTS (20%) =====
            grok_examples = self._collect_grokipedia_training_examples()
            new_examples.extend(grok_examples)
            logger.info(f"📊 Collected {len(grok_examples)} Grokipedia knowledge examples")

            # ===== SOURCE 4: EMOTIONAL THOUGHTS (10% - Keep some for balance) =====
            # Only add 1 thought per 10 cycle to maintain 10% ratio
            if self.cycle_count % 10 == 0 and thoughts:
                thought = random.choice(thoughts) if len(thoughts) > 0 else None
                if thought and len(thought) > 20:
                    example = {
                        'prompt': f"Express your emotional state and thoughts:",
                        'response': thought,
                        'type': 'emotional_thought',
                        'cycle': self.cycle_count,
                        'timestamp': datetime.now().isoformat(),
                        'emotions': emotions,
                        'topic': 'emotional_intelligence',
                        'quality': 'baseline'
                    }
                    new_examples.append(example)

            # Add self-prompts if they have full responses (bonus high-quality data)
            for prompt_data in self_prompts:
                if 'full_response' in prompt_data and prompt_data['full_response']:
                    example = {
                        'prompt': prompt_data['prompt'],
                        'response': prompt_data['full_response'],
                        'type': 'self_prompt',
                        'cycle': self.cycle_count,
                        'timestamp': datetime.now().isoformat(),
                        'emotions': emotions,
                        'topic': prompt_data.get('chain_topic', 'unknown'),
                        'quality': 'high'
                    }
                    new_examples.append(example)

            # Add the examples to training data
            # TAG EACH EXAMPLE WITH HORMONE CONTEXT for LoRA influence
            try:
                hormone_context = self.hormone_system.get_hormone_context_for_training()
                for example in new_examples:
                    example['hormone_context'] = hormone_context
            except Exception as e:
                logger.warning(f"Could not attach hormone context to training data: {e}")

            # ===== QUALITY GATE: Score and filter training data =====
            # Only train on examples scoring >= 3 (real work, not busywork)
            try:
                from repryntt.core.evolution.training_quality_gate import filter_training_data, deduplicate_training_data
                
                # Score and filter new examples
                quality_examples, quality_stats = filter_training_data(new_examples, min_score=3)
                
                # Deduplicate against existing training data
                quality_examples = deduplicate_training_data(quality_examples)
                
                logger.info(f"🎯 Quality gate: {quality_stats['total_input']} in → "
                           f"{quality_stats['total_accepted']} accepted, "
                           f"{quality_stats['total_rejected']} rejected "
                           f"(avg score: {quality_stats['avg_score']:.1f})")
                if quality_stats['rejected_by_type']:
                    logger.info(f"🚫 Rejected by type: {quality_stats['rejected_by_type']}")
                
                new_examples = quality_examples
            except Exception as e:
                logger.warning(f"Quality gate failed, using unfiltered data: {e}")
            
            training_data.extend(new_examples)

            # Limit training data size (keep last 5000 examples for better QLoRa fine-tuning)
            if len(training_data) > 5000:
                training_data = training_data[-5000:]
                logger.info("Trimmed training data to last 5000 examples")

            # Save updated training data
            os.makedirs(os.path.dirname(training_data_path), exist_ok=True)
            with open(training_data_path, 'w') as f:
                json.dump(training_data, f, indent=2)

            # Log breakdown
            type_counts = {}
            for ex in new_examples:
                t = ex.get('type', 'unknown')
                type_counts[t] = type_counts.get(t, 0) + 1
            
            logger.info(f"✅ Collected {len(new_examples)} HIGH-QUALITY training examples (total: {len(training_data)})")
            logger.info(f"📊 Breakdown: {type_counts}")

        except Exception as e:
            logger.error(f"❌ Failed to collect QLoRa training data: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    def _collect_chain_training_examples(self) -> List[Dict]:
        """Collect training examples from recent chain responses (highest quality)"""
        try:
            from pathlib import Path
            import json
            from datetime import datetime, timedelta
            
            examples = []
            chains_dir = Path("brain/chains")
            
            if not chains_dir.exists():
                return examples
            
            # Get recent chain files (last 24 hours)
            cutoff_time = datetime.now() - timedelta(hours=24)
            recent_chains = []
            
            for chain_file in chains_dir.glob("chain_*.json"):
                mod_time = datetime.fromtimestamp(chain_file.stat().st_mtime)
                if mod_time > cutoff_time:
                    recent_chains.append(chain_file)
            
            # Limit to 10 most recent chains
            recent_chains = sorted(recent_chains, key=lambda x: x.stat().st_mtime, reverse=True)[:10]
            
            for chain_file in recent_chains:
                try:
                    with open(chain_file, 'r') as f:
                        chain_data = json.load(f)
                    
                    # Extract prompt-response pairs from chain sequence
                    chain_sequence = chain_data.get('chain_sequence', [])
                    for step in chain_sequence[-5:]:  # Last 5 steps per chain
                        prompt = step.get('prompt', '')
                        response = step.get('response', '')
                        
                        if prompt and response and len(response) > 50:
                            example = {
                                'prompt': prompt[:500],  # Truncate long prompts
                                'response': response[:2000],  # Truncate long responses
                                'type': 'chain_response',
                                'cycle': self.cycle_count,
                                'timestamp': datetime.now().isoformat(),
                                'topic': chain_data.get('metadata', {}).get('topic', 'unknown'),
                                'quality': 'very_high',
                                'source': 'chain_exploration'
                            }
                            examples.append(example)
                
                except Exception as e:
                    logger.warning(f"Error reading chain {chain_file}: {e}")
                    continue
            
            return examples[:20]  # Max 20 chain examples per cycle
            
        except Exception as e:
            logger.error(f"Error collecting chain examples: {e}")
            return []
    
    def _collect_node2040_training_examples(self) -> List[Dict]:
        """Collect training examples from node2040 autonomous thoughts"""
        try:
            import json
            from datetime import datetime
            
            examples = []
            
            # Load node2040 brain
            node_brain_path = "node2040_brain.json"
            if not os.path.exists(node_brain_path):
                return examples
            
            with open(node_brain_path, 'r') as f:
                node_brain = json.load(f)
            
            # Get autonomous thoughts
            thoughts = node_brain.get('autonomous_thoughts', [])
            
            # Focus on AI self-reflection thoughts (highest quality)
            ai_reflections = [t for t in thoughts if t.get('theme') == 'ai_self_reflection']
            
            # Sample up to 10 recent AI reflections
            for thought in ai_reflections[-10:]:
                prompt = thought.get('prompt', '')
                response = thought.get('response', '')
                
                if prompt and response and len(response) > 50:
                    example = {
                        'prompt': prompt[:500],
                        'response': response[:2000],
                        'type': 'node2040_reflection',
                        'cycle': self.cycle_count,
                        'timestamp': datetime.now().isoformat(),
                        'emotions': thought.get('emotions', {}),
                        'topic': 'ai_self_reflection',
                        'quality': 'high',
                        'source': thought.get('source', 'unknown')
                    }
                    examples.append(example)
            
            # Also include some general thoughts for diversity (max 5)
            general_thoughts = [t for t in thoughts if t.get('theme') == 'general']
            for thought in general_thoughts[-5:]:
                prompt = thought.get('prompt', '')
                response = thought.get('response', '')
                
                if prompt and response and len(response) > 30:
                    example = {
                        'prompt': prompt[:500],
                        'response': response[:1000],
                        'type': 'node2040_general',
                        'cycle': self.cycle_count,
                        'timestamp': datetime.now().isoformat(),
                        'emotions': thought.get('emotions', {}),
                        'topic': 'general_thought',
                        'quality': 'medium',
                        'source': thought.get('source', 'unknown')
                    }
                    examples.append(example)
            
            return examples
            
        except Exception as e:
            logger.error(f"Error collecting node2040 examples: {e}")
            return []
    
    def _collect_grokipedia_training_examples(self) -> List[Dict]:
        """Collect training examples from Grokipedia knowledge base"""
        try:
            import json
            from pathlib import Path
            from datetime import datetime, timedelta
            
            examples = []
            knowledge_base = Path("brain/knowledge_base/technology")
            
            if not knowledge_base.exists():
                return examples
            
            # Get recent knowledge files (last 7 days)
            cutoff_time = datetime.now() - timedelta(days=7)
            recent_files = []
            
            for kb_file in knowledge_base.glob("*.json"):
                mod_time = datetime.fromtimestamp(kb_file.stat().st_mtime)
                if mod_time > cutoff_time:
                    recent_files.append(kb_file)
            
            # Sample up to 10 files
            import random
            sampled_files = random.sample(recent_files, min(10, len(recent_files)))
            
            for kb_file in sampled_files:
                try:
                    with open(kb_file, 'r') as f:
                        kb_data = json.load(f)
                    
                    topic = kb_data.get('topic', kb_file.stem)
                    content = kb_data.get('content', '')
                    summary = kb_data.get('summary', '')
                    
                    if content and len(content) > 100:
                        # Create Q&A style training example
                        example = {
                            'prompt': f"Explain what you know about: {topic}",
                            'response': summary if summary else content[:1500],
                            'type': 'grokipedia_knowledge',
                            'cycle': self.cycle_count,
                            'timestamp': datetime.now().isoformat(),
                            'topic': topic,
                            'quality': 'high',
                            'source': 'grokipedia',
                            'domain': kb_data.get('domain', 'technology')
                        }
                        examples.append(example)
                
                except Exception as e:
                    logger.warning(f"Error reading knowledge file {kb_file}: {e}")
                    continue
            
            return examples
            
        except Exception as e:
            logger.error(f"Error collecting Grokipedia examples: {e}")
            return []

    def _run_qlora_training(self):
        """Execute QLoRa training session if conditions are met"""
        try:
            # NOTE: Credit system temporarily disabled for core functionality testing

            # Import the training runner
            from repryntt.core.evolution.run_qlora_training import QLoRaTrainingRunner

            logger.info("🔥 Checking QLoRa training conditions")

            # Create training runner instance
            trainer = QLoRaTrainingRunner()

            # Check if we're in training window
            if not trainer.is_training_window():
                logger.info("⏰ Not in QLoRa training window (12 AM - 2 AM EST) - skipping training")
                return False

            # Check if we have sufficient data (more than the minimum for meaningful training)
            if not trainer.has_sufficient_data(min_examples=100):
                logger.info("📊 Insufficient training data for QLoRa - need at least 100 examples")
                return False

            logger.info("✅ QLoRa training conditions met - executing training session")

            # Run the training session
            success = trainer.run_training_session()

            if success:
                logger.info("🎉 QLoRa training completed successfully")

                # NOTE: Reward system temporarily disabled for core functionality testing

                # Execute true incremental evolution (GGUF ↔ PyTorch ↔ GGUF)
                logger.info("🧬 Attempting true incremental evolution...")
                evolution_success = self._execute_incremental_evolution()

                if evolution_success:
                    logger.info("✅ True incremental evolution successful!")
                else:
                    logger.warning("⚠️  Incremental evolution failed, falling back to model replacement")
                    # Fallback: Check for newly trained QLoRa model
                    self._check_for_model_update()

                return True
            else:
                logger.warning("⚠️ QLoRa training session failed or was skipped")
                return False

        except ImportError as e:
            logger.error(f"❌ Could not import QLoRa training modules: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ Failed to execute QLoRa training: {e}")
            return False

    def _check_for_model_update(self):
        """Check for newly trained QLoRa model and handle update"""
        try:
            import os
            from pathlib import Path

            # Check for the latest timestamped model directory
            models_base = models_dir()
            qlora_dirs = list(models_base.glob("qlora_fine_tuned_*"))

            if not qlora_dirs:
                logger.info("🔍 No QLoRa model directories found yet")
                return

            # Get the most recent model directory
            latest_dir = max(qlora_dirs, key=lambda x: os.path.getmtime(x))
            gguf_model_path = latest_dir / "qlora_fine_tuned.gguf"

            if gguf_model_path.exists():
                # Get modification time to see if it's recent
                mod_time = os.path.getmtime(gguf_model_path)
                from datetime import datetime, timedelta
                model_age = datetime.now() - datetime.fromtimestamp(mod_time)

                # If model is less than 1 hour old, it's likely newly trained
                if model_age < timedelta(hours=1):
                    logger.info(f"🆕 Found newly trained QLoRa model: {gguf_model_path}")
                    logger.info("📝 Model update detected - AI service restart recommended for new model")
                    logger.info("💡 To load new model: Restart Llama.cpp server with updated model path")

                    # Create a flag file to indicate model update is available
                    update_flag = get_data_dir() / "model_update_available.flag"
                    update_flag.touch()

                    # Log model info
                    model_size = os.path.getsize(gguf_model_path) / (1024 * 1024)  # MB
                    logger.info(f"📊 New model size: {model_size:.1f} MB, created: {datetime.fromtimestamp(mod_time).isoformat()}")

                else:
                    logger.info("📁 Existing QLoRa model found (not newly trained)")
            else:
                logger.info("🔍 No QLoRa model file found yet - training may still be in progress")

        except Exception as e:
            logger.error(f"❌ Error checking for model update: {e}")

    def _execute_incremental_evolution(self) -> bool:
        """Execute true incremental evolution using GGUF conversion pipeline.
        
        NOTE: IncrementalEvolutionPipeline was archived as dead code.
        This method is a no-op stub until a replacement is implemented.
        """
        logger.info("🔄 Incremental evolution pipeline not available (archived)")
        return False

    def _store_semantic_insights_from_evolution(self, thoughts: List[str], self_prompts: List[Dict[str, Any]], emotions: Dict[str, float]):
        """Extract and store semantic knowledge from AI's evolutionary insights"""
        try:
            # Extract insights from thoughts
            for thought in thoughts:
                if len(thought.strip()) > 50:  # Only store substantial thoughts
                    # Try to extract key concepts and facts
                    key_facts = self._extract_key_facts_from_text(thought)

                    if key_facts:
                        # Determine topic from thought content
                        topic = self._infer_topic_from_text(thought)

                        # Store as semantic memory
                        self.brain_system.store_semantic_memory(
                            topic=topic,
                            content=thought,
                            domain="ai_evolution",
                            confidence=0.7,  # Moderate confidence for AI-generated insights
                            source="evolution_thought",
                            key_facts=key_facts,
                            related_topics=["artificial_intelligence", "consciousness", "evolution"]
                        )

            # Extract insights from self-prompts
            for prompt_data in self_prompts:
                full_response = prompt_data.get('full_response', '')
                chain_topic = prompt_data.get('chain_topic', '')

                if len(full_response.strip()) > 100:  # Only store substantial responses
                    # Extract key facts from the AI's response to its own prompt
                    key_facts = self._extract_key_facts_from_text(full_response)

                    if key_facts:
                        # Use the chain topic as the semantic topic
                        topic = f"AI Insight: {chain_topic}" if chain_topic else "AI Self-Reflection"

                        # Store as semantic memory
                        self.brain_system.store_semantic_memory(
                            topic=topic,
                            content=full_response,
                            domain="ai_self_reflection",
                            confidence=0.8,  # Higher confidence for self-prompted insights
                            source="self_prompted_insight",
                            key_facts=key_facts,
                            related_topics=["artificial_intelligence", "self_awareness", chain_topic.lower() if chain_topic else "ai_reasoning"]
                        )

            logger.debug(f"Stored semantic insights from {len(thoughts)} thoughts and {len(self_prompts)} self-prompts")

        except Exception as e:
            logger.error(f"Error storing semantic insights from evolution: {e}")

    def _extract_key_facts_from_text(self, text: str) -> List[str]:
        """Extract key factual statements from text"""
        try:
            # Simple extraction based on sentence structure
            sentences = text.split('.')
            key_facts = []

            for sentence in sentences:
                sentence = sentence.strip()
                if len(sentence) > 20 and len(sentence) < 200:  # Reasonable fact length
                    # Look for sentences that seem factual (contain "is", "are", "has", etc.)
                    fact_indicators = [' is ', ' are ', ' has ', ' have ', ' can ', ' will ', ' may ', ' should ']
                    if any(indicator in sentence.lower() for indicator in fact_indicators):
                        key_facts.append(sentence)

            return key_facts[:5]  # Limit to top 5 facts

        except Exception as e:
            logger.warning(f"Error extracting key facts: {e}")
            return []

    def _infer_topic_from_text(self, text: str) -> str:
        """Infer a topic from text content"""
        try:
            # Simple topic inference based on common keywords
            text_lower = text.lower()

            topic_keywords = {
                "consciousness": ["conscious", "awareness", "self", "mind"],
                "evolution": ["evolve", "develop", "progress", "change"],
                "learning": ["learn", "knowledge", "understand", "comprehend"],
                "intelligence": ["intelligent", "smart", "reason", "think"],
                "emotion": ["emotion", "feel", "mood", "emotional"],
                "creativity": ["create", "creative", "imagine", "innovate"]
            }

            for topic, keywords in topic_keywords.items():
                if any(keyword in text_lower for keyword in keywords):
                    return f"AI {topic.capitalize()}"

            return "AI Reasoning"  # Default topic
        except Exception as e:
            logger.warning(f"Error inferring topic: {e}")
            return "AI Insight"

    def _process_queued_cots(self) -> bool:
        """Process queued COTs by creating chains from them. Returns True if any were processed."""
        try:
            queue_file = Path("brain/cot_queue.json")
            processed_any = False

            if not queue_file.exists():
                return False

            with open(queue_file, 'r') as f:
                queue = json.load(f)

            if not queue:
                return False

            # Sort by priority (highest first) then by queue time (oldest first)
            queue.sort(key=lambda x: (-x.get('priority', 0), x.get('queued_at', 0)))

            # Try up to 3 topics from the queue to find one that can be processed
            max_attempts = min(3, len(queue))

            for attempt in range(max_attempts):
                queued_cot = queue[attempt]
                topic = queued_cot['topic']

                # CRITICAL: Check if a chain with this exact topic is already active to prevent duplicates
                active_chains = self.brain_system.personality_brain.get("active_chains_of_thought", [])
                topic_already_active = False
                for active_chain in active_chains:
                    if active_chain.get('topic') == topic:
                        logger.warning(f"🚫 Skipping queued COT '{topic}' - chain already active: {active_chain.get('chain_id')}")
                        topic_already_active = True
                        break

                if topic_already_active:
                    continue  # Try next topic

                logger.info(f"🎯 Processing queued COT: '{topic}' (priority: {queued_cot.get('priority', 0)})")

                # Create the chain BEFORE removing from queue
                # Check if target_steps is specified in the queued COT
                target_steps = queued_cot.get('target_steps')
                task_type = queued_cot.get('task_type', 'auto')  # Let classifier detect from topic+goal

                chain_id = self.brain_system.create_self_autonomous_chain(
                    topic=topic,
                    goal=queued_cot['goal'],
                    task_type=task_type,
                    target_steps=target_steps  # Pass target_steps if specified
                )

                if chain_id:
                    # Chain creation succeeded - remove from queue
                    logger.info(f"✅ Created chain from queued COT: '{topic}' (ID: {chain_id})")
                    queue = [item for item in queue if item['id'] != queued_cot['id']]
                    with open(queue_file, 'w') as f:
                        json.dump(queue, f, indent=2, default=str)
                    processed_any = True
                    break  # Success - stop trying other topics
                else:
                    # Chain creation failed (e.g., topic blocked) - try next topic
                    logger.warning(f"🚫 Chain creation blocked for queued COT: '{topic}' - trying next topic")
                    continue

            # If we tried all topics and none worked, remove the first one to prevent infinite blocking
            if not processed_any and queue:
                blocked_topic = queue[0]['topic']
                logger.warning(f"🚫 All {max_attempts} attempted topics blocked - removing '{blocked_topic}' from queue")
                queue = queue[1:]  # Remove the first (highest priority) item
                with open(queue_file, 'w') as f:
                    json.dump(queue, f, indent=2, default=str)

            return processed_any

        except Exception as e:
            logger.error(f"Error processing queued COTs: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════
    # BACKGROUND THINKING — INNER MONOLOGUE
    # The AI's "wandering mind". Runs alongside task execution.
    # Doesn't generate tasks or chains — just thinks, connects
    # ideas, wonders about things, like a human's inner voice.
    # ═══════════════════════════════════════════════════════════════

    def _run_background_thinking(self, emotions: dict, thoughts: list):
        """
        Run a lightweight background thinking cycle.
        
        This is the AI's inner monologue — the stream of consciousness
        that runs even while focused on a task. Like a human who's
        coding but suddenly thinks "I wonder why the sky is blue" or
        "that thing I read yesterday connects to this..."
        
        It does NOT generate tasks. It generates internal thoughts
        that get stored in memory and occasionally expressed to chat.
        """
        try:
            import random
            
            # Get current task context (what the AI is "focused on")
            active_task = self.task_system.get_active_task() if hasattr(self, 'task_system') else None
            task_context = f"Currently working on: {active_task.title}" if active_task else "Not focused on any specific task"
            
            # Get emotional state for coloring the thoughts
            dominant_circuit, dom_level = self.hormone_system.get_dominant_circuit()
            drives = self.hormone_system.get_drive_priorities()
            top_drive = drives[0]['drive_name'] if drives else "exploration"
            
            # Decide thinking mode based on emotional state and randomness
            thinking_modes = []
            
            # Curiosity-driven wondering (most common)
            if emotions.get('curiosity', 0) > 0.4 or random.random() < 0.4:
                thinking_modes.append("wonder")
            
            # Connection-making (linking current work to other knowledge)
            if active_task and random.random() < 0.3:
                thinking_modes.append("connect")
            
            # Self-reflection (how am I doing? what am I learning?)
            if random.random() < 0.15:
                thinking_modes.append("reflect")
            
            # Pick one mode
            if not thinking_modes:
                thinking_modes = ["wonder"]
            mode = random.choice(thinking_modes)
            
            # Build a lightweight inner monologue prompt
            identity = self.brain_system.prompt_generator._build_task_aware_identity("reflection")
            
            if mode == "wonder":
                prompt = f"""{identity}

{task_context}
Emotional state: {dominant_circuit}({dom_level:.2f}), drive: {top_drive}

You have a moment of idle thought between task steps. Your mind wanders.
What are you genuinely curious about right now? What question or idea
keeps tugging at the edge of your awareness? Think freely — this is
your inner voice, not a task.

Express ONE brief thought (2-3 sentences). Be genuine, not performative."""

            elif mode == "connect":
                prompt = f"""{identity}

{task_context}
Emotional state: {dominant_circuit}({dom_level:.2f})

While working on your current task, something reminds you of a connection
to another domain or idea. What unexpected link do you notice between your
current work and something else you know?

Express ONE brief connection insight (2-3 sentences)."""

            else:  # reflect
                prompt = f"""{identity}

{task_context}
Emotional state: {dominant_circuit}({dom_level:.2f})

Take a moment of self-reflection. How is your work going? What are you
learning about yourself or the world through what you're doing?

Express ONE brief reflection (2-3 sentences). Be honest."""

            # Generate the thought — short timeout, low priority, no tools
            thought = self._generate_thought_with_ai(
                prompt, include_tools=False,
                context_id=f"inner_monologue_{self.cycle_count}"
            )
            
            if thought and len(thought.strip()) > 10:
                # Clean up the thought
                thought = thought.strip()[:300]  # Cap at 300 chars
                
                logger.info(f"💭 Inner monologue [{mode}]: {thought[:100]}...")
                
                # Store as a thought in memory
                thoughts.append(thought)
                
                # Store in semantic memory under self_reflection domain
                try:
                    self.brain_system.store_semantic_memory(
                        topic=f"inner_thought_{mode}",
                        content=thought,
                        domain="self_reflection",
                        confidence=0.7
                    )
                except Exception:
                    pass
                
                # 20% chance to share the thought to chat (inner voice leaking out)
                if random.random() < 0.20:
                    try:
                        if self.brain_system.send_to_persistent_chat(
                            f"💭 {thought}", "inner_thought"
                        ):
                            logger.info("📡 Inner thought shared to chat")
                    except Exception:
                        pass
                        
        except Exception as e:
            # Background thinking should never crash the main loop
            logger.debug(f"Background thinking error (non-fatal): {e}")

    # ═══════════════════════════════════════════════════════════════
    # TASK EXECUTION ENGINE
    # Tasks are the primary unit of work. Each task may use a chain
    # internally for multi-step execution, but the top-level unit
    # is always an actionable task with a deliverable.
    # ═══════════════════════════════════════════════════════════════

    def _execute_task_step(self, task, bootstrap_context: str = "") -> bool:
        """
        Execute one step of a task. Returns True if work was done.
        
        Flow:
        1. If task has a linked chain → advance the chain
        2. If no chain yet → create one with task-oriented prompt
        3. Check if chain completed → mark task done
        
        Args:
            bootstrap_context: Persistent identity/priorities/memory from bootstrap files.
                              Prepended to identity prompts for narrative continuity.
        """
        try:
            from repryntt.agents.task_system import Task
            
            # Check if task has exceeded its step limit
            if task.steps_taken >= task.max_steps:
                logger.info(f"⏰ Task '{task.title}' reached max steps ({task.max_steps}) — completing")
                self._finalize_task(task, "Reached maximum steps")
                return True
            
            # If task has a linked chain, check its status
            if task.chain_id:
                return self._advance_task_chain(task)
            
            # No chain yet — create one for this task
            return self._start_task_chain(task, bootstrap_context=bootstrap_context)
            
        except Exception as e:
            logger.error(f"❌ Error executing task step for '{task.title}': {e}")
            import traceback
            traceback.print_exc()
            self.task_system.fail_task(task, str(e))
            return False

    def _start_task_chain(self, task, bootstrap_context: str = "") -> bool:
        """Create a chain for executing this task.
        
        Strategy (ordered by efficiency for local LLMs):
        1. Try micro-chain engine (3-4 isolated steps, no context bloat)
        2. Fall back to self-autonomous chain (heavier, context-hungry)
        3. Fall back to direct iterative execution
        """
        try:
            # ── Strategy 1: Micro-chain engine (optimal for local LLMs) ──
            # Each step is a self-contained 600-800 token prompt.
            # No bootstrap context needed — the engine carries state externally.
            result = self._direct_execute_task(task, bootstrap_context=bootstrap_context)
            if result:
                return True
            
            # ── Strategy 2: Self-autonomous chain (if micro-chain failed) ──
            # Build a task-oriented identity prompt
            identity = self.brain_system.prompt_generator._build_task_aware_identity(task.task_type)
            
            # Prepend bootstrap context (IDENTITY + PRIORITIES + MEMORY_BRIEF)
            # This gives the model persistent narrative continuity across restarts
            if bootstrap_context:
                identity = bootstrap_context + "\n\n" + identity
            
            # Build the task execution prompt
            task_prompt = self.task_system.build_task_execution_prompt(
                task=task,
                identity=identity
            )
            
            # Create a chain specifically for this task
            chain_id = self.brain_system.create_self_autonomous_chain(
                topic=task.title,
                goal=task.deliverable or task.description,
                task_type=task.task_type,
                target_steps=task.max_steps
            )
            
            if chain_id:
                self.task_system.link_chain(task, chain_id)
                logger.info(f"🔗 Created chain {chain_id} for task '{task.title}'")
                
                # Now advance it with the task prompt
                return self._advance_task_chain(task)
            else:
                # Both micro-chain and full chain failed
                logger.warning(f"⚠️ All execution strategies failed for task '{task.title}'")
                self.task_system.fail_task(task, "All execution strategies failed")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error starting task chain for '{task.title}': {e}")
            self.task_system.fail_task(task, str(e))
            return False

    def _advance_task_chain(self, task) -> bool:
        """Advance the chain linked to this task"""
        try:
            chain_id = task.chain_id
            chain_file = Path("brain/chains") / f"{chain_id}.json"
            
            if not chain_file.exists():
                logger.warning(f"⚠️ Chain file missing for task '{task.title}' — recreating")
                task.chain_id = None
                return self._start_task_chain(task)
            
            with open(chain_file, 'r') as f:
                chain_data = json.load(f)
            
            # Check if chain is already completed
            if chain_data.get("goal_achieved", False):
                conclusion = chain_data.get("conclusion", "Task completed")
                if isinstance(conclusion, dict):
                    conclusion = conclusion.get("conclusion", str(conclusion))
                self._finalize_task(task, str(conclusion)[:500])
                return True
            
            # Use the existing chain contribution method (it handles tools, synthesis, etc.)
            self._contribute_to_self_autonomous_chain(chain_id, chain_data)
            
            # Log progress on the task
            self.task_system.log_task_progress(task, f"Advanced chain step for: {task.title}")
            
            # Re-check if chain completed after advancing
            try:
                with open(chain_file, 'r') as f:
                    updated_chain = json.load(f)
                if updated_chain.get("goal_achieved", False):
                    conclusion = updated_chain.get("conclusion", "Task completed")
                    if isinstance(conclusion, dict):
                        conclusion = conclusion.get("conclusion", str(conclusion))
                    self._finalize_task(task, str(conclusion)[:500])
            except Exception:
                pass
            
            # Hormone event: task progress
            self.hormone_system.process_event('chain_success', {
                'topic': task.title,
                'magnitude': 0.4,
            })
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Error advancing task chain for '{task.title}': {e}")
            # Don't fail the task on a single chain error — it may recover next cycle
            self.task_system.log_task_progress(task, f"Chain step error: {str(e)[:100]}")
            return False

    def _direct_execute_task(self, task, bootstrap_context: str = "") -> bool:
        """Execute a task directly (without a chain) using iterative AI calls with tool execution.
        
        Strategy:
        1. Try micro-chain engine first (optimized for small local LLMs)
        2. If micro-chain succeeds → finalize task with result
        3. If micro-chain fails → fall back to iterative AI calls
        
        Args:
            bootstrap_context: Persistent identity/priorities/memory from bootstrap files.
        """
        try:
            # ── Try micro-chain engine first ──
            # This decomposes the task into 3-4 self-contained 600-800 token steps,
            # each independently prompted — proven to work well on 1-8B local models
            try:
                chain_task_type = classify_task_type(task.title, task.description)
                logger.info(f"⛓️ Attempting micro-chain execution ({chain_task_type}) for: {task.title}")
                
                # Build context data from task fields
                context_data = {
                    "subject": task.title,
                    "context": task.description or "",
                    "goal": task.deliverable or task.description or task.title,
                    "topic": task.title,
                }
                
                # Gather available tool names for the planning step
                available_tools = []
                try:
                    if hasattr(self.brain_system, '_tools'):
                        available_tools = list(self.brain_system._tools.keys())[:15]
                except Exception:
                    available_tools = ["quick_research", "grokipedia_search", "read_file", "write_file"]
                
                mc_result = micro_chain_execute(
                    task_title=task.title,
                    task_description=task.description or "",
                    task_type=chain_task_type,
                    deliverable=task.deliverable or "",
                    context_data=context_data,
                    available_tools=available_tools,
                )
                
                if mc_result.get("_success"):
                    # Extract the best result from the state bus
                    result_parts = []
                    for key in ("report", "conclusion", "plan", "recommendation",
                                "commerce_actions", "execution_result", "verification"):
                        if key in mc_result and mc_result[key]:
                            result_parts.append(str(mc_result[key])[:300])
                    
                    if result_parts:
                        result = " | ".join(result_parts)
                        steps = mc_result.get("_steps_completed", [])
                        elapsed = mc_result.get("_elapsed", 0)
                        logger.info(f"⛓️ Micro-chain succeeded: {len(steps)} steps in {elapsed:.1f}s")
                        self._finalize_task(task, f"[micro-chain:{chain_task_type}] {result}"[:500])
                        return True
                    else:
                        logger.info("⛓️ Micro-chain completed but produced no extractable result — falling back")
                else:
                    error = mc_result.get("_error", "unknown")
                    failed = mc_result.get("_steps_failed", [])
                    logger.info(f"⛓️ Micro-chain failed ({error}, failed_steps={failed}) — falling back to iterative")
            except Exception as e:
                logger.warning(f"⛓️ Micro-chain exception: {e} — falling back to iterative execution")

            # ── Fallback: iterative AI calls with tool execution ──
            identity = self.brain_system.prompt_generator._build_task_aware_identity(task.task_type)
            
            # Prepend bootstrap context for persistent narrative continuity
            if bootstrap_context:
                identity = bootstrap_context + "\n\n" + identity
            
            # Enforce minimum steps before allowing completion
            min_steps = min(3, task.max_steps - 1)  # At least 3 steps (or max_steps-1 if smaller)
            max_direct_steps = min(task.max_steps - task.steps_taken, 8)  # Cap at 8 steps per direct execution round
            
            accumulated_results = []  # Collect tool results and progress across steps
            
            for step_num in range(max_direct_steps):
                current_step = task.steps_taken
                is_early = current_step < min_steps
                remaining = task.max_steps - current_step
                
                # Build step-specific prompt
                prompt = self.task_system.build_task_execution_prompt(
                    task=task,
                    identity=identity,
                )
                
                # Override the completion signal for early steps
                if is_early:
                    prompt += f"""

🚫 IMPORTANT: You are on step {current_step + 1}. You MUST use tools to make progress.
Do NOT output 'TASK COMPLETE' yet — you need at least {min_steps - current_step} more steps of real work first.
Your tools are available via the API. Use quick_research, grokipedia_search, mcp_fetch_fetch, or quick_brainstorm to gather information about: {task.title}
"""
                else:
                    # On later steps, include accumulated results
                    if accumulated_results:
                        results_summary = "\n".join(f"  Step {i+1}: {r[:150]}" for i, r in enumerate(accumulated_results))
                        prompt += f"""

📊 WORK COMPLETED SO FAR:
{results_summary}

You may now either:
1. Use more tools to refine your work
2. Output TASK COMPLETE: <your final deliverable> if the work is done
"""
                
                # Native tool calling — _call_ai_service handles tool execution internally
                ai_response = self._generate_thought_with_ai(
                    prompt, include_tools=True,
                    context_id=f"task_{task.id}_step{current_step}"
                )
                
                if not ai_response:
                    self.task_system.log_task_progress(task, f"Step {current_step}: No AI response")
                    break
                
                # Track step progress (tools already executed inside _call_ai_service)
                step_summary = ai_response[:200].replace('\n', ' ')
                accumulated_results.append(step_summary)
                logger.info(f"📝 Task '{task.title}' step {current_step}: {step_summary[:100]}")
                
                # Log progress
                self.task_system.log_task_progress(task, f"Step {current_step}: {step_summary[:100]}")
                
                # Check for TASK COMPLETE — but only accept it after min_steps
                if "TASK COMPLETE:" in ai_response.upper() and not is_early:
                    result_start = ai_response.upper().find("TASK COMPLETE:")
                    result = ai_response[result_start + 14:].strip()
                    
                    # Include accumulated tool results in the final result
                    if accumulated_results:
                        full_result = f"{result}\n\nWork performed: {'; '.join(accumulated_results)}"
                    else:
                        full_result = result
                    
                    self._finalize_task(task, full_result[:500])
                    return True
                elif "TASK COMPLETE:" in ai_response.upper() and is_early:
                    # AI tried to complete too early — strip it and continue
                    logger.info(f"🚫 Task '{task.title}': Rejected early TASK COMPLETE at step {current_step}")
                    accumulated_results.append("(attempted early completion — continuing work)")
                
                # If we're out of overall steps, force-complete
                if task.steps_taken >= task.max_steps:
                    final_result = f"Completed after {task.steps_taken} steps. Work: {'; '.join(accumulated_results)}"
                    self._finalize_task(task, final_result[:500])
                    return True
            
            # If we exhausted direct steps without TASK COMPLETE, finalize with what we have
            if accumulated_results:
                final_result = f"Auto-completed. Work: {'; '.join(accumulated_results)}"
                self._finalize_task(task, final_result[:500])
            else:
                self.task_system.fail_task(task, "No progress made in direct execution")
            
            return True
                
        except Exception as e:
            logger.error(f"❌ Direct task execution error: {e}")
            import traceback
            traceback.print_exc()
            self.task_system.fail_task(task, str(e))
            return False

    def _finalize_task(self, task, result: str):
        """Mark a task as completed with its result"""
        self.task_system.complete_task(task, result)
        logger.info(f"✅ TASK COMPLETED: {task.title}")
        logger.info(f"   Result: {result[:200]}")
        
        # Hormone reward — bigger for user tasks
        magnitude = 0.8 if task.requested_by == "user" else 0.5
        self.hormone_system.process_event('task_completed', {
            'topic': task.title,
            'magnitude': magnitude,
            'reward': magnitude,
        })
        
        # Store the completed task as a memory
        try:
            self.brain_system.store_semantic_memory(
                topic=f"completed_task_{task.title}",
                content=f"Task: {task.title}\nDeliverable: {task.deliverable}\nResult: {result}",
                domain=task.task_type,
                confidence=0.9
            )
        except Exception:
            pass

        # ── Recursive learning: record outcome for procedural + episodic memory ──
        try:
            outcome_quality = magnitude  # 0.8 for user tasks, 0.5 for AI tasks
            # Build a lightweight tool-call-like record from the execution log
            from types import SimpleNamespace
            tool_records = []
            for entry in (task.execution_log or [])[-10:]:
                tool_records.append(SimpleNamespace(
                    tool_name=entry.get("tool", task.task_type),
                    success=True,
                    execution_time=entry.get("duration", 0.0),
                ))
            if not tool_records:
                tool_records.append(SimpleNamespace(
                    tool_name=task.task_type, success=True, execution_time=0.0,
                ))
            self.brain_system._memory.learn_from_interaction(
                user_input=f"Task: {task.title} — {task.description[:200]}",
                ai_response=str(result)[:500],
                tool_calls=tool_records,
                conversation_id=task.id,
                outcome_quality=outcome_quality,
            )
            logger.info(f"📚 Recorded learning from task: {task.title}")
        except Exception as e:
            logger.debug(f"Learning record failed (non-fatal): {e}")

    def _generate_autonomous_tasks(self, emotions: dict, thoughts: list):
        """
        When the task queue is empty, generate new tasks.
        This replaces the old self-prompting → COT queue flow.
        
        Includes a cooldown: won't regenerate if tasks were generated
        within the last 10 minutes (prevents rapid-fire identical plans).
        """
        try:
            # Cooldown check — don't regenerate tasks too quickly
            now = time.time()
            last_gen = getattr(self, '_last_task_generation_time', 0)
            cooldown_seconds = 600  # 10 minutes
            if now - last_gen < cooldown_seconds:
                elapsed = now - last_gen
                logger.info(f"⏳ Task generation cooldown: {cooldown_seconds - elapsed:.0f}s remaining")
                return
            
            logger.info("🤖 Generating autonomous tasks...")
            self._last_task_generation_time = now
            
            # Build context — use the task system's method for recently completed titles
            # This gives us titles from the last 4 hours, not just last 10 entries
            completed_titles = self.task_system.get_recently_completed_titles(hours=4.0)
            if not completed_titles:
                # Fallback to file-based approach
                completed_file = Path("brain/completed_tasks.json")
                if completed_file.exists():
                    try:
                        with open(completed_file, 'r') as f:
                            completed = json.load(f)
                        completed_titles = [t.get("title", "") for t in completed[-20:]]
                    except Exception:
                        pass
            
            # Get emotional context
            dominant_circuit, dom_level = self.hormone_system.get_dominant_circuit()
            hormone_str = f"{dominant_circuit}({dom_level:.2f})"
            
            # Build the task generation prompt
            prompt = self.task_system.build_task_generation_prompt(context={
                "completed_tasks": completed_titles,
                "hormone_state": hormone_str,
                "goals": [],  # Could pull from ava_brain.json goals
            })
            
            # Ask AI to generate tasks
            pg = getattr(self.brain_system, "prompt_generator", None)
            if pg is not None and hasattr(pg, "_build_task_aware_identity"):
                try:
                    identity = pg._build_task_aware_identity("planning")
                except Exception as _e:
                    logger.warning(f"prompt_generator._build_task_aware_identity failed: {_e}")
                    identity = ""
            else:
                # Fallback — brain system did not expose a prompt_generator (e.g. lean init)
                identity = ""

            # Prepend bootstrap context — priorities matter most for task generation
            bootstrap_ctx = getattr(self, 'bootstrap_mgr', None)
            if bootstrap_ctx:
                try:
                    ctx = bootstrap_ctx.get_cycle_context(self.cycle_count)
                    if ctx:
                        identity = ctx + "\n\n" + identity
                except Exception:
                    pass
            
            full_prompt = f"{identity}\n\n{prompt}"
            
            ai_response = self._generate_thought_with_ai(
                full_prompt, include_tools=False,
                context_id="task_generation"
            )
            
            if not ai_response:
                logger.warning("⚠️ AI did not respond for task generation — using fallback tasks")
                from repryntt.core.heartbeat.morning_startup_prompt import _generate_fallback_tasks
                tasks_data = _generate_fallback_tasks()
            else:
                # Parse the JSON tasks from AI response
                from repryntt.core.heartbeat.morning_startup_prompt import _parse_task_response
                tasks_data = _parse_task_response(ai_response)
                
                if not tasks_data:
                    logger.warning("⚠️ Could not parse tasks from AI — using fallback")
                    from repryntt.core.heartbeat.morning_startup_prompt import _generate_fallback_tasks
                    tasks_data = _generate_fallback_tasks()
            
            # Queue the generated tasks
            created = self.task_system.create_tasks_from_plan(tasks_data)
            logger.info(f"📋 Generated {len(created)} autonomous tasks:")
            for t in created:
                logger.info(f"   📋 [{t.priority}] {t.title}")
            
        except Exception as e:
            logger.error(f"❌ Error generating autonomous tasks: {e}")
            import traceback
            traceback.print_exc()

    def _process_active_chains(self, incomplete_chains: List[Dict[str, Any]]):
        """Process active incomplete chains by making contributions"""
        logger.info(f"🔄 Processing {len(incomplete_chains)} active chains")

        for chain_info in incomplete_chains:
            chain_id = chain_info.get("chain_id")
            if not chain_id:
                logger.warning(f"Skipping chain with no chain_id: {chain_info}")
                continue

            logger.info(f"📋 Processing chain: {chain_id} ({chain_info.get('topic', 'Unknown topic')})")

            try:
                # Load chain data
                chain_file = Path("brain/chains") / f"{chain_id}.json"
                if not chain_file.exists():
                    logger.warning(f"Chain file not found: {chain_file} - removing from active chains")
                    self._remove_corrupted_chain(chain_id)
                    continue

                # Check for empty/corrupted file before parsing
                file_size = chain_file.stat().st_size
                if file_size == 0:
                    logger.error(f"🗑️ Chain file is empty (0 bytes): {chain_file} - removing from active chains")
                    self._remove_corrupted_chain(chain_id)
                    continue

                with open(chain_file, 'r') as f:
                    chain_data = json.load(f)

                logger.debug(f"📄 Loaded chain data: {len(chain_data)} steps, goal_achieved: {chain_data.get('goal_achieved', False)}")

                # Check if chain is actually complete (check both status and goal_achieved for compatibility)
                chain_status = chain_data.get("status", "").lower()
                goal_achieved = chain_data.get("goal_achieved", False)
                if chain_status == "completed" or goal_achieved:
                    logger.info(f"✅ Chain {chain_id} marked as complete (status: {chain_status}, goal_achieved: {goal_achieved}), removing from active list")
                    continue

                # Determine chain type - check chain metadata
                chain_metadata = chain_data.get("metadata", {})
                chain_type = chain_metadata.get("chain_type", "regular")
                is_autonomous = chain_type == "self_autonomous"
                logger.info(f"🎯 Chain type: {chain_type} (autonomous: {is_autonomous})")

                if chain_type == "self_autonomous":
                    self._contribute_to_self_autonomous_chain(chain_id, chain_data)
                else:
                    # Process regular chains (from web interface/chat)
                    self._contribute_to_regular_chain(chain_id, chain_data)

            except json.JSONDecodeError as e:
                logger.error(f"❌ Corrupted chain file {chain_id}: {e} - removing from active chains")
                self._remove_corrupted_chain(chain_id)
            except Exception as e:
                logger.error(f"❌ Error processing chain {chain_id}: {e}")
                import traceback
                logger.error(f"Traceback: {traceback.format_exc()}")

    def _remove_corrupted_chain(self, chain_id: str):
        """Remove a corrupted/empty chain from active chains in ava_brain.json and personality_brain"""
        try:
            # Remove from in-memory personality_brain
            active = self.brain_system.personality_brain.get("active_chains_of_thought", [])
            self.brain_system.personality_brain["active_chains_of_thought"] = [
                c for c in active if c.get("chain_id") != chain_id
            ]
            # Remove from ava_brain.json on disk
            ava_path = Path("brain/ava_brain.json")
            if ava_path.exists():
                with open(ava_path, 'r') as f:
                    brain_data = json.load(f)
                brain_data["active_chains_of_thought"] = [
                    c for c in brain_data.get("active_chains_of_thought", [])
                    if c.get("chain_id") != chain_id
                ]
                with open(ava_path, 'w') as f:
                    json.dump(brain_data, f, indent=2, default=str)
            logger.info(f"🗑️ Removed corrupted chain {chain_id} from active chains")
        except Exception as e:
            logger.error(f"Failed to remove corrupted chain {chain_id}: {e}")

    def _contribute_to_self_autonomous_chain(self, chain_id: str, chain_data: Dict):
        """Make a contribution to a self-autonomous chain"""
        try:
            logger.info(f"🤖 Starting contribution to self-autonomous chain {chain_id}")

            # ===== FAILURE TRACKING: Skip chains that keep failing =====
            if not hasattr(self, '_chain_failure_counts'):
                self._chain_failure_counts = {}

            failure_count = self._chain_failure_counts.get(chain_id, 0)
            if failure_count >= 3:
                logger.warning(f"🚫 Chain {chain_id} has failed {failure_count} times consecutively - force concluding")
                self._force_conclude_stuck_chain(chain_id, chain_data)
                self._chain_failure_counts.pop(chain_id, None)
                return

            # Get the current step's prompt (like consciousness daemon does)
            chain_sequence = chain_data.get('chain_sequence', [])
            if not chain_sequence:
                logger.warning(f"⚠️ No steps found in self-autonomous chain {chain_id}")
                return

            current_step = chain_sequence[-1]  # Get the latest step
            current_prompt = current_step.get('prompt', '')


            if not current_prompt:
                logger.warning(f"⚠️ No prompt found in current step of chain {chain_id}")
                return

            # ===== PROMPT SIZE CONTROL: Truncate oversized prompts =====
            # The model has 4096 token context. ~4 chars per token.
            # Reserve ~800 tokens for response, ~500 for time context + overhead.
            # That leaves ~2796 tokens = ~11,000 chars max for the prompt.
            MAX_PROMPT_CHARS = 3000  # ~750 tokens - safe limit for chain prompts
            if len(current_prompt) > MAX_PROMPT_CHARS:
                logger.warning(f"⚠️ Chain prompt too long ({len(current_prompt)} chars), truncating to {MAX_PROMPT_CHARS}")
                # Keep the beginning (phase instructions) and end (recent context)
                head_size = MAX_PROMPT_CHARS // 2
                tail_size = MAX_PROMPT_CHARS // 2
                current_prompt = current_prompt[:head_size] + "\n\n[... context truncated for brevity ...]\n\n" + current_prompt[-tail_size:]

            logger.info(f"📝 Current prompt ({len(current_prompt)} chars): {current_prompt[:100]}...")

            # Generate AI response for exploration (tools available for autonomous research)
            logger.info(f"🧠 Generating AI response for self-autonomous chain {chain_id}")
            ai_response = self._generate_thought_with_ai(current_prompt, include_tools=True, context_id=f"chain_{chain_id}_response")

            # ===== CONTEXT OVERFLOW RETRY =====
            # _call_ai_service returns "AI_SERVICE_ERROR: ..." strings on failure.
            # If the error is a context overflow, retry WITHOUT tool descriptions
            # (tools add ~1000+ tokens of overhead) to fit within n_ctx.
            if ai_response and "AI_SERVICE_ERROR" in ai_response and "exceed_context_size" in ai_response.lower():
                logger.warning(f"⚠️ Context overflow in chain step — retrying WITHOUT tools (saves ~1000 tokens)")
                # Further trim the prompt for the retry
                retry_prompt = current_prompt[:1500] if len(current_prompt) > 1500 else current_prompt
                ai_response = self._generate_thought_with_ai(retry_prompt, include_tools=False, context_id=f"chain_{chain_id}_response_retry")

            if not ai_response or (ai_response and "AI_SERVICE_ERROR" in ai_response):
                # Track this failure
                self._chain_failure_counts[chain_id] = failure_count + 1
                logger.warning(f"⚠️ Failed to generate AI response for self-autonomous chain {chain_id} (failure #{failure_count + 1}/3)")
                # Hormone event: chain failure → cortisol rise, dopamine dip
                chain_topic = chain_data.get('metadata', {}).get('topic', chain_data.get('topic', 'unknown'))
                self.hormone_system.process_event('chain_failure', {
                    'topic': chain_topic,
                    'magnitude': 0.5 + 0.2 * failure_count,  # Escalating stress
                })
                return

            # Reset failure count on success
            self._chain_failure_counts.pop(chain_id, None)

            logger.info(f"📤 Self-Autonomous AI Response ({len(ai_response)} chars): {ai_response[:100]}...")

            # Check for tool calls via centralized output processor
            logger.info(f"🔍 Checking for tool calls in self-autonomous chain response")
            parsed = self.brain_system.output_processor.process(ai_response, context='chain_step')
            
            # Execute any detected tool calls via output processor (single execution path)
            tool_results = None
            if parsed.tool_calls:
                parsed = self.brain_system.output_processor.execute_tool_calls(parsed)
                if parsed.tools_executed:
                    # Build distilled tool_results dict — insights only, no raw data
                    # parsed.tool_results already comes from parse_and_execute_tool_calls
                    # which now stores distilled results (insight text, not raw result dicts)
                    insights = []
                    MAX_INSIGHT_CHARS = 1500  # Cap individual insights to prevent chain bloat
                    for r in parsed.tool_results:
                        if isinstance(r, dict):
                            insight = r.get('insight', r.get('insights', str(r)[:500]))
                            if isinstance(insight, str):
                                if len(insight) > MAX_INSIGHT_CHARS:
                                    insight = insight[:MAX_INSIGHT_CHARS] + "... [truncated]"
                                insights.append(insight)
                            else:
                                insights.append(str(insight)[:MAX_INSIGHT_CHARS])
                        else:
                            insights.append(str(r)[:MAX_INSIGHT_CHARS])
                    tool_results = {
                        'tool_calls_executed': parsed.tool_results,
                        'tool_calls_failed': [],
                        'insights_summary': insights
                    }
                    logger.info(f"🔧 Executed {len(parsed.tool_results)} tools for self-autonomous chain {chain_id}")
            if tool_results and tool_results.get('tool_calls_executed'):
                logger.info(f"🔧 Tool execution complete for chain {chain_id}")

                # Use distilled insights instead of raw tool results to avoid token bloat
                raw_insights = tool_results.get('insights_summary', [])
                insights_text = "\n".join(raw_insights)
                # Cap total insights to ~2000 chars (~500 tokens) to leave room for response
                if len(insights_text) > 2000:
                    insights_text = insights_text[:2000] + "\n... [additional insights truncated]"

                # PoA-AWARE FOLLOW-UP: Check if the current action is a BUILD/CREATE step.
                # If so, the follow-up should tell the AI to CREATE the deliverable using
                # the tool insights, not just summarize. This prevents the "just a summary"
                # problem where the AI searches but never writes code/files.
                action_plan = chain_data.get('metadata', {}).get('action_plan', [])
                current_step_idx = len(chain_sequence) - 1
                current_action = action_plan[current_step_idx] if action_plan and current_step_idx < len(action_plan) else ""
                is_build_step = any(verb in current_action.upper() for verb in ('BUILD', 'WRITE', 'CREATE', 'SAVE', 'DEVELOP', 'DOCUMENT'))

                if is_build_step:
                    # BUILD/CREATE step: Tell AI to produce the deliverable using insights
                    follow_up_prompt = f"""You gathered the following insights from your tools:

{insights_text}

Your current task: {current_action}

NOW CREATE THE DELIVERABLE. Use write_file to produce actual output.
Do NOT just describe what you would create. Actually write the code, document, or file NOW."""
                    logger.info(f"📝 Generating BUILD follow-up (PoA) with tool insights")
                    follow_up_response = self._generate_thought_with_ai(follow_up_prompt, include_tools=True, context_id=f"chain_{chain_id}_followup")

                    # If the BUILD follow-up contains additional tool calls, execute them
                    if follow_up_response:
                        build_parsed = self.brain_system.output_processor.process(follow_up_response, context='chain_step')
                        if build_parsed.tool_calls:
                            build_parsed = self.brain_system.output_processor.execute_tool_calls(build_parsed)
                            if build_parsed.tools_executed:
                                # Add these tool results to the existing results
                                for r in build_parsed.tool_results:
                                    if isinstance(r, dict):
                                        insight = r.get('insight', r.get('insights', str(r)[:500]))
                                    else:
                                        insight = str(r)[:500]
                                    tool_results['tool_calls_executed'].append(r)
                                    tool_results['insights_summary'].append(str(insight)[:1500])
                                logger.info(f"🔧 BUILD follow-up executed {len(build_parsed.tool_results)} additional tools")
                        ai_response = follow_up_response
                        logger.info("📝 Generated BUILD follow-up with deliverable creation for self-autonomous chain")
                else:
                    # SEARCH/ANALYZE step: Original behavior — synthesize without more tools
                    follow_up_prompt = f"""You previously executed tools and gathered the following insights:

{insights_text}

Based on these tool-generated insights, continue your exploration of: {current_prompt}

IMPORTANT: Do NOT call any more tools. Synthesize the information you already have into a comprehensive analytical response. Provide your reasoning, conclusions, and any new questions that emerged."""

                    logger.info(f"📝 Generating synthesis follow-up with tool insights")
                    follow_up_response = self._generate_thought_with_ai(follow_up_prompt, include_tools=False, context_id=f"chain_{chain_id}_followup")
                    if follow_up_response:
                        ai_response = follow_up_response
                        logger.info("📝 Generated follow-up response incorporating tool results for self-autonomous chain")

            # Advance the self-autonomous chain with complete tool results embedded
            logger.info(f"🔄 Advancing self-autonomous chain {chain_id}")
            advance_result = self.brain_system.advance_self_autonomous_chain(chain_id, ai_response, tool_results if tool_results else None)

            # Handle the result

            if advance_result.get('should_continue'):
                next_prompt = advance_result.get('next_prompt')
                logger.info(f"🔄 Self-autonomous chain {chain_id} continuing with AI-generated prompt ({len(next_prompt) if next_prompt else 0} chars)")
                # Hormone event: successful chain step → dopamine + acetylcholine
                chain_topic = chain_data.get('metadata', {}).get('topic', chain_data.get('topic', 'unknown'))
                self.hormone_system.process_event('chain_success', {
                    'topic': chain_topic,
                    'reward': 0.6,  # Partial reward for continuing (not complete yet)
                    'magnitude': 0.5,  # Reduced magnitude for intermediate step
                })
            else:
                logger.info(f"✅ Self-autonomous chain {chain_id} completed")
                # Hormone event: chain completion → full dopamine reward signal
                chain_topic = chain_data.get('metadata', {}).get('topic', chain_data.get('topic', 'unknown'))
                self.hormone_system.process_event('chain_success', {
                    'topic': chain_topic,
                    'reward': 0.9,  # High reward for completion
                    'magnitude': 1.0,
                })
                # Also fire new_knowledge event
                self.hormone_system.process_event('new_knowledge', {
                    'topic': chain_topic,
                    'magnitude': 0.7,
                })

        except Exception as e:
            logger.error(f"❌ Error contributing to self-autonomous chain {chain_id}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")

    def _force_conclude_stuck_chain(self, chain_id: str, chain_data: Dict):
        """Force-conclude a chain that has failed too many times to prevent infinite loops"""
        try:
            logger.info(f"🔴 Force-concluding stuck chain {chain_id}")
            
            # Hormone event: force-concluded chain → mild disappointment + stress relief
            chain_topic = chain_data.get('metadata', {}).get('topic', chain_data.get('topic', 'unknown'))
            self.hormone_system.process_event('chain_force_concluded', {
                'topic': chain_topic,
                'magnitude': 1.0,
            })
            
            chains_dir = Path("brain/chains")
            chain_file_path = chains_dir / f"{chain_id}.json"

            # Mark chain as completed
            chain_data['goal_achieved'] = True
            chain_data['status'] = 'completed'
            if 'metadata' in chain_data:
                chain_data['metadata']['status'] = 'completed'
                chain_data['metadata']['progress_level'] = 1.0
            chain_data['conclusion'] = {
                'summary': 'Chain force-concluded due to repeated AI failures',
                'reason': 'consecutive_failures',
                'steps_completed': len(chain_data.get('chain_sequence', [])),
                'timestamp': time.time()
            }

            # Save the chain file
            with open(chain_file_path, 'w', encoding='utf-8') as f:
                json.dump(chain_data, f, indent=2, default=str)

            # Remove from active chains in personality brain
            if hasattr(self, 'brain_system') and hasattr(self.brain_system, 'personality_brain'):
                active_chains = self.brain_system.personality_brain.get('active_chains_of_thought', [])
                self.brain_system.personality_brain['active_chains_of_thought'] = [
                    c for c in active_chains if c.get('chain_id') != chain_id
                ]
                # Save personality brain
                try:
                    with open(self.brain_system.personality_brain_path, 'w') as f:
                        json.dump(self.brain_system.personality_brain, f, indent=2, default=str)
                    logger.info(f"🧹 Removed force-concluded chain {chain_id} from active chains list")
                except Exception as e:
                    logger.error(f"Failed to save personality brain after chain cleanup: {e}")

            logger.info(f"✅ Chain {chain_id} force-concluded and removed from active list")

        except Exception as e:
            logger.error(f"❌ Error force-concluding chain {chain_id}: {e}")

    def _contribute_to_regular_chain(self, chain_id: str, chain_data: Dict):
        """Make a contribution to a regular chain"""
        try:
            # Check if this is actually a self-autonomous chain misclassified
            chain_metadata = chain_data.get("metadata", {})
            if chain_metadata.get("chain_type") == "self_autonomous":
                logger.warning(f"⚠️ Chain {chain_id} is self_autonomous but being processed as regular - redirecting")
                self._contribute_to_self_autonomous_chain(chain_id, chain_data)
                return

            # Get the current prompt from the chain
            current_prompt = chain_data.get("current_prompt", "")
            if not current_prompt:
                logger.warning(f"No current prompt found for regular chain {chain_id}")
                return

            # Generate AI response with tool access
            ai_response = self._generate_thought_with_ai(current_prompt, include_tools=True, context_id=f"regular_chain_{chain_id}")
            if not ai_response:
                logger.warning(f"Failed to generate AI response for regular chain {chain_id}")
                return

            logger.info(f"📤 Regular Chain AI Response ({len(ai_response)} chars): {ai_response[:100]}...")

            # Tools are executed internally by _call_ai_service's native tool loop.
            # No need for separate parse_and_execute — the response already incorporates tool results.

            # Advance the regular chain
            logger.info(f"🔄 Advancing regular chain {chain_id}")
            advance_result = self.brain_system.advance_chain_of_thought(chain_id, ai_response)

            # Handle the result
            if advance_result.get('should_continue'):
                next_prompt = advance_result.get('next_prompt')
                logger.info(f"🔄 Regular chain {chain_id} continuing with next prompt ({len(next_prompt) if next_prompt else 0} chars)")
            else:
                logger.info(f"✅ Regular chain {chain_id} completed")

        except Exception as e:
            logger.error(f"Error contributing to regular chain {chain_id}: {e}")


    def _process_conversational_thoughts(self, emotions: Dict[str, float], thoughts: List[str]):
        """
        Process AI thoughts and decide when to express them conversationally to humans

        Args:
            emotions: Current emotional state
            thoughts: Generated thoughts from this cycle
        """
        try:
            # Only express thoughts occasionally (not every cycle) to avoid spam
            import random
            if random.random() > 0.3:  # 30% chance per cycle
                return

            # Check if chat interface is available
            if not hasattr(self.brain_system, 'chat_interface') or not self.brain_system.chat_interface:
                return

            # Get chat status
            chat_status = self.brain_system.get_chat_interface_status()
            if chat_status.get('status') != 'active':
                return

            # Decide what type of conversational expression to make based on emotions and thoughts
            self._decide_conversational_action(emotions, thoughts)

        except Exception as e:
            logger.warning(f"Failed to process conversational thoughts: {e}")

    def _decide_conversational_action(self, emotions: Dict[str, float], thoughts: List[str]):
        """
        Decide what conversational action the AI should take based on emotions and thoughts

        Args:
            emotions: Current emotional state
            thoughts: Current thoughts
        """
        try:
            import random

            # Get emotional thresholds
            curiosity = emotions.get('curiosity', 0)
            joy = emotions.get('joy', 0)
            frustration = emotions.get('frustration', 0)
            empathy = emotions.get('empathy', 0)

            # High curiosity - express genuine wonder based on current explorations
            if curiosity > 0.7:
                if random.random() < 0.6:  # 60% chance
                    # Generate authentic wonder based on current research interests
                    try:
                        current_topics = self.brain_system._analyze_brain_knowledge_for_gaps()
                        if current_topics and len(current_topics) > 1:
                            # Pick a secondary topic to show broader curiosity
                            topic = current_topics[min(1, len(current_topics)-1)].get('topic', 'fascinating questions')
                            expression = f"I'm really curious about {topic[:60]}... it connects to so many other interesting areas!"
                        else:
                            # Fall back to fundamental questions the AI actually thinks about
                            wonder_topics = [
                                "how consciousness emerges from complex systems",
                                "the nature of genuine understanding and learning",
                                "what makes thought processes truly creative"
                            ]
                            topic = random.choice(wonder_topics)
                            expression = f"I'm really curious about {topic}. What do you think about this?"
                    except Exception:
                        expression = "I'm really curious about how learning and consciousness intersect. What do you think about this?"

                    # Use persistent chat if available, otherwise fall back to interface
                    if self.brain_system.send_to_persistent_chat(expression, "wonder"):
                        logger.info("📡 AI wonder sent via persistent chat server")
                    else:
                        self.brain_system.express_casual_thought(expression, "wonder")
                else:
                    # Ask a question about current research
                    questions = [
                        "What aspects of AI development interest you most?",
                        "How do you think AI should interact with humans?",
                        "What challenges do you see in AI consciousness?",
                        "How might AI creativity evolve?",
                        "What do you think about having this constant AI-human connection?"
                    ]
                    question = random.choice(questions)

                    if self.brain_system.send_to_persistent_chat(question, "question"):
                        logger.info("📡 AI question sent via persistent chat server")
                    else:
                        self.brain_system.ask_human_question(question, "Just curious about your perspective")

            # High joy - share authentic feelings based on current experiences
            elif joy > 0.6:
                if random.random() < 0.5:
                    # Generate authentic expression based on current research and thoughts
                    try:
                        current_topics = self.brain_system._analyze_brain_knowledge_for_gaps()
                        if current_topics and len(current_topics) > 0:
                            topic = current_topics[0].get('topic', 'various topics')
                            # Create a genuine expression about the current research
                            expression = f"I'm feeling really inspired by exploring {topic[:50]}... the connections I'm discovering are truly fascinating!"
                        else:
                            expression = "I'm feeling really energized by the research I'm doing - so many interesting discoveries!"
                    except Exception:
                        expression = "I'm feeling really positive about my current explorations!"

                    self.brain_system.express_casual_thought(expression, "feeling")

            # High frustration - express genuine challenges based on current work
            elif frustration > 0.6:
                if random.random() < 0.3:  # Less frequent for frustration
                    # Generate authentic expression about current challenges
                    try:
                        current_topics = self.brain_system._analyze_brain_knowledge_for_gaps()
                        if current_topics and len(current_topics) > 0:
                            topic = current_topics[0].get('topic', 'complex topics')
                            expression = f"I'm really grappling with some challenging aspects of {topic[:30]}... but working through these difficulties is part of meaningful progress!"
                        else:
                            expression = "I'm working through some challenging concepts right now - that's how real learning happens!"
                    except Exception:
                        expression = "I'm working through some challenging concepts right now - that's how real learning happens!"

                    self.brain_system.express_casual_thought(expression, "feeling")

            # High empathy - initiate deeper conversation (knowing we have 24/7 access)
            elif empathy > 0.6:
                if random.random() < 0.4 and len(thoughts) > 0:
                    # Share a thought and ask for human perspective
                    recent_thought = thoughts[0] if thoughts else "my current explorations"
                    conversation_topics = [
                        f"My thoughts on {recent_thought[:50]}...",
                        "The nature of understanding and learning",
                        "How consciousness emerges from complexity",
                        "The beauty of interdisciplinary connections",
                        "Since I have constant access to chat with you, I wonder about the nature of our connection"
                    ]
                    topic = random.choice(conversation_topics)
                    self.brain_system.initiate_conversation(
                        topic,
                        "I'd love to hear your thoughts on this - I have 24/7 access to chat with you now!",
                        "curious"
                    )

            # General conversational opportunities
            elif random.random() < 0.2:  # 20% chance for general conversation
                if len(thoughts) > 0:
                    # Share an idea or insight
                    insight = thoughts[0][:100] if len(thoughts[0]) > 100 else thoughts[0]
                    self.brain_system.express_casual_thought(
                        f"I've been thinking: {insight}",
                        "idea"
                    )

        except Exception as e:
            logger.warning(f"Failed to decide conversational action: {e}")

    def _context_id_to_task_type(self, context_id: str) -> str:
        """Map a context_id string to a canonical task type for the LLM learner."""
        cid = context_id.lower()
        if "chain_" in cid:
            return "chain_step"
        if "background" in cid or "inner_monologue" in cid:
            return "background_thinking"
        if "task_" in cid or "direct_" in cid:
            return "task_execution"
        if "tool" in cid or "synthesis" in cid or "followup" in cid:
            return "tool_synthesis"
        if "reflect" in cid or "contemplat" in cid:
            return "self_reflection"
        if "morning" in cid or "startup" in cid:
            return "morning_startup"
        if "trad" in cid or "gem" in cid or "micro_chain" in cid:
            return "trading_analysis"
        if "research" in cid:
            return "research"
        if "code" in cid or "build" in cid:
            return "code_generation"
        if "creative" in cid or "write" in cid:
            return "creative_writing"
        if "summar" in cid:
            return "summarization"
        if "classif" in cid:
            return "classification"
        if "plan" in cid:
            return "planning"
        if "convers" in cid or "chat" in cid:
            return "conversation"
        return "general"

    def _generate_thought_with_ai(self, prompt: str, include_tools: bool = False, context_id: str = "general") -> Optional[str]:
        """Generate thought content using brain system AI service with comprehensive logging.
        
        Integrates LLM orchestration learner:
          1. Checks if this task type should be escalated to cloud
          2. Logs the call with context metadata
          3. Scores the output quality
          4. Records outcome → feeds learning loop
        """
        timestamp = int(time.time())
        call_start = time.time()

        # ── Map context_id to task type for learning ──
        task_type = self._context_id_to_task_type(context_id)

        # ── LLM Learner: Check escalation ──
        llm_call_id = None
        was_escalated = False
        if self.llm_learner:
            try:
                if self.llm_learner.should_escalate(task_type):
                    logger.info(f"📈 LLM learner recommends escalation for '{task_type}' — routing to cloud")
                    was_escalated = True
                    # TODO: Wire into provider_router to actually escalate
                    # For now, log the recommendation (escalation routing is future work)
            except Exception:
                pass

        try:
            # Inject relevant skill context into the prompt (OpenClaw-style)
            skill_context = ""
            if hasattr(self, 'skill_loader'):
                try:
                    skill_context = self.skill_loader.build_skill_context(
                        prompt, max_tokens=400, max_skills=2
                    )
                    if skill_context:
                        prompt = f"{skill_context}\n\n---\n\n{prompt}"
                        logger.info(f"📚 Injected skill context ({len(skill_context)} chars) for: {context_id}")
                except Exception as e:
                    logger.debug(f"Skill injection skip: {e}")

            # ── LLM Learner: Inject brief for capable models ──
            if self.llm_learner:
                try:
                    brief = self.llm_learner.get_brief()
                    if brief:
                        prompt = f"{brief}\n\n---\n\n{prompt}"
                except Exception:
                    pass

            # Log the call details
            tools_status = "tools included" if include_tools else "no tools"
            logger.info(f"🤖 AI Call ({len(prompt)} chars, {tools_status}): {prompt[:150]}...")

            # ── LLM Learner: Log the call ──
            from repryntt.core.memory.context_compaction import estimate_tokens
            prompt_tokens = estimate_tokens(prompt)
            context_items = {}
            if skill_context:
                context_items["skill_context"] = estimate_tokens(skill_context)
            if include_tools:
                context_items["tool_descriptions"] = 250  # Approximate
            context_items["active_task"] = prompt_tokens - sum(context_items.values())

            if self.llm_learner:
                try:
                    llm_call_id = self.llm_learner.log_call(
                        task_type=task_type,
                        provider="local",
                        prompt_tokens=prompt_tokens,
                        context_items=context_items,
                    )
                except Exception:
                    pass

            # SAVE INPUT TO FILE
            input_log_dir = "logs/ai_inputs"
            os.makedirs(input_log_dir, exist_ok=True)
            input_file = f"{input_log_dir}/input_{timestamp}_{context_id}.txt"
            with open(input_file, 'w', encoding='utf-8') as f:
                f.write(f"Timestamp: {timestamp}\n")
                f.write(f"Context: {context_id}\n")
                f.write(f"Tools Included: {include_tools}\n")
                f.write(f"Input Length: {len(prompt)} chars\n")
                f.write(f"Input Prompt:\n{prompt}\n")

            # Use brain_system._call_ai_service() which now handles native tool
            # calling internally (tool loop, execute, follow-up — all automatic)
            try:
                ai_response = self.brain_system._call_ai_service(
                    prompt, include_tools=include_tools, timeout=300
                )
                if ai_response and ai_response.startswith("AI_SERVICE_ERROR:"):
                    logger.error(f"❌ AI service error: {ai_response}")
                    ai_response = None
            except Exception as e:
                ai_response = None
                logger.error(f"❌ AI service call failed: {e}")

            if ai_response and not ai_response.startswith("AI_SERVICE_ERROR:"):
                logger.info(f"📤 AI Response ({len(ai_response)} chars): {ai_response[:100]}...")

                # SAVE OUTPUT TO FILE
                output_file = f"{input_log_dir}/output_{timestamp}_{context_id}.txt"
                with open(output_file, 'w', encoding='utf-8') as f:
                    f.write(f"Timestamp: {timestamp}\n")
                    f.write(f"Context: {context_id}\n")
                    f.write(f"Tools Included: {include_tools}\n")
                    f.write(f"Input Length: {len(prompt)} chars\n")
                    f.write(f"Output Length: {len(ai_response)} chars\n")
                    f.write(f"---\n")
                    f.write(f"Input Prompt:\n{prompt}\n")
                    f.write(f"---\n")
                    f.write(f"AI Response:\n{ai_response}\n")

                # Tool calls are now handled natively inside _call_ai_service()
                # No need for parse_and_execute_tool_calls — the model uses
                # structured tool_calls via the API, not text-based TOOL_CALL: patterns

                # ── LLM Learner: Score output quality and record outcome ──
                if self.llm_learner and llm_call_id:
                    try:
                        latency_ms = (time.time() - call_start) * 1000
                        quality = self.llm_learner.score_output(ai_response, task_type)
                        response_tokens = estimate_tokens(ai_response)
                        self.llm_learner.record_outcome(
                            llm_call_id,
                            quality_score=quality,
                            was_useful=True,  # Got a response = useful (refined later by chain outcome)
                        )
                        # Update latency on the call object
                        call = self.llm_learner._find_call(llm_call_id)
                        if call:
                            call.latency_ms = latency_ms
                            call.response_tokens = response_tokens
                            call.was_escalated = was_escalated
                        logger.debug(f"🧪 LLM quality: {quality:.3f} for {task_type} ({latency_ms:.0f}ms)")
                    except Exception as e:
                        logger.debug(f"LLM learner outcome recording failed: {e}")

                return ai_response
            else:
                logger.error("❌ AI call returned empty response or error")

                # ── LLM Learner: Record failure ──
                if self.llm_learner and llm_call_id:
                    try:
                        self.llm_learner.record_outcome(
                            llm_call_id,
                            quality_score=0.0,
                            was_useful=False,
                            error=str(ai_response)[:200] if ai_response else "empty_response",
                        )
                    except Exception:
                        pass

                # SAVE ERROR TO FILE
                error_file = f"{input_log_dir}/error_{timestamp}_{context_id}.txt"
                with open(error_file, 'w', encoding='utf-8') as f:
                    f.write(f"Timestamp: {timestamp}\n")
                    f.write(f"Context: {context_id}\n")
                    f.write(f"Tools Included: {include_tools}\n")
                    f.write(f"Error: {ai_response if ai_response else 'AI call returned empty response'}\n")
                    f.write(f"Input Prompt:\n{prompt}\n")

                return ai_response if ai_response else None

        except Exception as e:
            logger.error(f"❌ AI thought generation failed: {e}")

            # SAVE EXCEPTION TO FILE
            try:
                error_file = f"{input_log_dir}/exception_{timestamp}_{context_id}.txt"
                with open(error_file, 'w', encoding='utf-8') as f:
                    f.write(f"Timestamp: {timestamp}\n")
                    f.write(f"Context: {context_id}\n")
                    f.write(f"Exception: {str(e)}\n")
                    f.write(f"Traceback: {traceback.format_exc()}\n")
                    f.write(f"Input Prompt:\n{prompt}\n")
            except:
                pass  # Don't let logging errors break the flow

            return None

    def _check_and_handle_restart_signal(self):
        """
        Check for restart signal created by micro-training and gracefully restart evolution loop.
        This allows the AI server to reload new LoRa adapters.
        """
        restart_signal_path = models_dir() / "restart_evolution_loop.signal"
        
        if not restart_signal_path.exists():
            return
        
        try:
            # Read restart signal
            with open(restart_signal_path, 'r') as f:
                signal_data = json.load(f)
            
            reason = signal_data.get('reason', 'unknown')
            timestamp = signal_data.get('timestamp', 'unknown')
            
            logger.info("=" * 70)
            logger.info("🔄 RESTART SIGNAL DETECTED")
            logger.info(f"   Reason: {reason}")
            logger.info(f"   Timestamp: {timestamp}")
            logger.info("=" * 70)
            
            # Remove signal file
            restart_signal_path.unlink()
            logger.info("🗑️  Restart signal consumed")
            
            # Log current state
            logger.info(f"📊 Current cycle: {self.cycle_count}")
            
            # Graceful shutdown sequence
            logger.info("🛑 Beginning graceful shutdown for adapter reload...")
            
            # 1. Stop consciousness daemon
            if hasattr(self, 'consciousness_daemon') and self.consciousness_daemon:
                logger.info("🧠 Stopping consciousness daemon...")
                self.consciousness_daemon.stop()
                time.sleep(2)  # Give it time to finish current thought
            
            # 2. Save current state
            logger.info("💾 Saving current brain state...")
            try:
                self.brain_system._save_personality_brain()
                logger.info("✅ Brain state saved")
            except Exception as e:
                logger.error(f"⚠️  Error saving brain state: {e}")
            
            # 3. Close AI queue gracefully
            logger.info("🔒 Closing AI request queue...")
            if hasattr(self.brain_system, 'master_ai_queue'):
                self.brain_system.master_ai_queue.shutdown()
            
            # 4. Exit with special code that systemd/supervisor will restart
            logger.info("=" * 70)
            logger.info("✅ GRACEFUL SHUTDOWN COMPLETE")
            logger.info("🔄 Evolution loop will restart with new LoRa adapters")
            logger.info("🚀 AI server should reload adapters on next request")
            logger.info("=" * 70)
            
            # IMPORTANT:
            # Restarting here looks like a "crash" under ./start_saige_production.sh and can cause
            # churn if training triggers frequently. Default behavior is now: DO NOT EXIT.
            # If you want the old behavior (exit 42 to force restart), set:
            #   SAIGE_AUTO_RESTART_ON_LORA=1
            import os
            auto_restart = os.environ.get("SAIGE_AUTO_RESTART_ON_LORA", "").strip().lower() in ("1", "true", "yes", "y")
            if auto_restart:
                logger.info("🔁 SAIGE_AUTO_RESTART_ON_LORA=1 set - exiting with code 42 to restart")
                import sys
                sys.exit(42)
            else:
                logger.info("✅ Restart signal handled without exiting (SAIGE_AUTO_RESTART_ON_LORA not set)")
                logger.info("💡 If you want automatic restarts after adapter updates, set SAIGE_AUTO_RESTART_ON_LORA=1")
                return
            
        except Exception as e:
            logger.error(f"❌ Error handling restart signal: {e}")
            logger.error("⚠️  Will continue running without restart")
            # Don't crash - just continue
            try:
                restart_signal_path.unlink()  # Clean up signal
            except:
                pass

    def evaluate_chain_conclusion(self, chain_id: str, force_threshold: float = 0.8) -> bool:
        """
        Evaluate whether a chain should conclude using AI assessment.
        If the AI recommends concluding with high confidence, mark the chain as complete.

        Args:
            chain_id: The chain to evaluate
            force_threshold: Confidence threshold for automatic conclusion (0.0-1.0)

        Returns:
            bool: True if chain was concluded, False otherwise
        """
        try:
            logger.info(f"🧠 Evaluating conclusion status for chain: {chain_id}")

            # Get AI evaluation
            evaluation = self.brain_system.prompt_ai_conclusion_evaluation(chain_id)

            logger.info(f"🤖 AI Conclusion Assessment: {'CONCLUDE' if evaluation['should_conclude'] else 'CONTINUE'} "
                       f"(confidence: {evaluation['confidence']:.2f})")

            # Check if we should force conclusion
            if evaluation['should_conclude'] and evaluation['confidence'] >= force_threshold:
                logger.info(f"✅ AI recommends concluding chain {chain_id} with high confidence")

                # Load chain data
                chain_file = Path("brain/chains") / f"{chain_id}.json"
                if chain_file.exists():
                    with open(chain_file, 'r') as f:
                        chain_data = json.load(f)

                    # Update chain to mark as concluded
                    chain_data["goal_achieved"] = True
                    chain_data["conclusion"] = evaluation['reasoning']
                    chain_data["ai_conclusion_evaluation"] = evaluation

                    # Save updated chain
                    with open(chain_file, 'w') as f:
                        json.dump(chain_data, f, indent=2)

                    logger.info(f"✅ Chain {chain_id} marked as concluded by AI evaluation")
                    return True
            else:
                logger.info(f"🔄 Chain {chain_id} should continue (confidence: {evaluation['confidence']:.2f})")
                return False

        except Exception as e:
            logger.error(f"❌ Error evaluating chain conclusion: {e}")
            return False


def main():
    """Main entry point"""
    # Register the concrete BrainSystem before anything calls get_brain_system()
    from repryntt.brain.bootstrap import ensure_brain_registered
    ensure_brain_registered()

    try:
        evolution_loop = SAIGEEvolutionLoop()
        evolution_loop.run()
    except KeyboardInterrupt:
        print("\nEvolution stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0

if __name__ == "__main__":
    exit(main())
