#!/usr/bin/env python3
"""
Feeder Coordinator - SAIGE Learning Pipeline Orchestrator
Coordinates all feeders and manages the integrated learning pipeline
Real implementation with stimulus aggregation and brain system integration
"""

import json
import os
import sys
import time
import logging
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, asdict
import numpy as np
from collections import deque, defaultdict
import queue
import signal
import psutil

logger = logging.getLogger(__name__)

@dataclass
class FeederStatus:
    name: str
    pid: Optional[int]
    status: str  # 'running', 'stopped', 'error', 'starting'
    last_update: float
    last_stimulus: Dict[str, float]
    error_count: int
    restart_count: int
    health_score: float  # 0-1, overall health
    process_handle: Optional[Any] = None

@dataclass
class AggregatedStimulus:
    timestamp: float
    individual_stimuli: Dict[str, Dict[str, float]]  # feeder_name -> hormone_values
    aggregated_values: Dict[str, float]  # Final hormone values
    confidence_scores: Dict[str, float]  # Confidence in each hormone value
    dominant_sources: Dict[str, str]  # Which feeder dominated each hormone
    coordination_notes: List[str]  # Notes about coordination decisions

@dataclass
class LearningDirective:
    directive_id: str
    priority: float  # 0-1
    directive_type: str  # 'exploration', 'consolidation', 'adaptation', 'optimization'
    description: str
    target_feeders: List[str]
    expected_duration: float  # hours
    success_criteria: Dict[str, Any]
    created_timestamp: float
    status: str  # 'pending', 'active', 'completed', 'failed'

class FeederCoordinator:
    """
    Master coordinator for all SAIGE feeders and learning pipeline
    """
    
    def __init__(self, config_path: str = "config/feeder_coordinator.json"):
        self.config = self._load_config(config_path)
        
        # Feeder management
        self.feeders = {
            'conversation': FeederStatus('conversation', None, 'stopped', 0, {}, 0, 0, 0.0),
            'web_research': FeederStatus('web_research', None, 'stopped', 0, {}, 0, 0, 0.0),
            'sensor': FeederStatus('sensor', None, 'stopped', 0, {}, 0, 0, 0.0),
            'news': FeederStatus('news', None, 'stopped', 0, {}, 0, 0, 0.0),
            'performance': FeederStatus('performance', None, 'stopped', 0, {}, 0, 0, 0.0),
            'curiosity': FeederStatus('curiosity', None, 'stopped', 0, {}, 0, 0, 0.0)
        }
        
        # Stimulus coordination
        self.stimulus_history = deque(maxlen=200)
        self.aggregation_weights = self._initialize_aggregation_weights()
        
        # Learning coordination
        self.active_directives = {}  # directive_id -> LearningDirective
        self.coordination_strategy = 'adaptive'  # 'balanced', 'focused', 'adaptive'
        
        # Brain system integration
        self.brain_interface = BrainInterface(self.config.get('brain_config', {}))
        
        # Monitoring and health
        self.system_health = {}
        self.coordination_metrics = {
            'total_cycles': 0,
            'successful_aggregations': 0,
            'failed_coordinations': 0,
            'average_response_time': 0.0,
            'brain_integration_success_rate': 0.0
        }
        
        # Shutdown handling
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        self.shutdown_requested = False
    
    def _load_config(self, config_path: str) -> Dict:
        """Load configuration or create default"""
        default_config = {
            "coordination": {
                "stimulus_aggregation_interval": 30,  # seconds
                "feeder_health_check_interval": 60,   # seconds
                "brain_integration_interval": 45,     # seconds
                "directive_planning_interval": 300,   # 5 minutes
                "max_restart_attempts": 3,
                "feeder_timeout": 120,                # 2 minutes
                "aggregation_strategy": "adaptive"    # weighted, balanced, adaptive
            },
            "feeders": {
                "conversation": {
                    "enabled": True,
                    "script": "conversation_feeder.py",
                    "weight": 0.8,
                    "priority": 0.9,
                    "auto_restart": True
                },
                "web_research": {
                    "enabled": True,
                    "script": "web_research_feeder.py", 
                    "weight": 0.7,
                    "priority": 0.8,
                    "auto_restart": True
                },
                "sensor": {
                    "enabled": True,
                    "script": "sensor_feeder.py",
                    "weight": 0.6,
                    "priority": 0.7,
                    "auto_restart": True
                },
                "news": {
                    "enabled": True,
                    "script": "news_feeder.py",
                    "weight": 0.5,
                    "priority": 0.6,
                    "auto_restart": True
                },
                "performance": {
                    "enabled": True,
                    "script": "performance_feeder.py",
                    "weight": 0.9,
                    "priority": 1.0,
                    "auto_restart": True
                },
                "curiosity": {
                    "enabled": True,
                    "script": "curiosity_feeder.py",
                    "weight": 0.8,
                    "priority": 0.8,
                    "auto_restart": True
                }
            },
            "brain_config": {
                "brain_memory_file": "brain/working_memory.json",
                "long_term_memory_file": "brain/long_term_memory.json",
                "stimulus_integration_mode": "weighted_average",
                "memory_update_threshold": 0.3,
                "consolidation_trigger_threshold": 0.7
            },
            "learning_coordination": {
                "enable_adaptive_priorities": True,
                "enable_cross_feeder_learning": True,
                "enable_directive_planning": True,
                "learning_phase_adaptation": True
            },
            "output": {
                "aggregated_stimulus": "data/aggregated_stimulus.json",
                "coordination_status": "data/coordination_status.json",
                "learning_directives": "data/learning_directives.json",
                "system_health": "data/system_health.json"
            }
        }
        
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return {**default_config, **json.load(f)}
        else:
            os.makedirs(os.path.dirname(config_path), exist_ok=True)
            with open(config_path, 'w') as f:
                json.dump(default_config, f, indent=2)
            return default_config
    
    def _initialize_aggregation_weights(self) -> Dict[str, Dict[str, float]]:
        """Initialize hormone aggregation weights for each feeder"""
        return {
            'conversation': {
                'adrenaline': 0.2,
                'serotonin': 0.8,   # Conversation strongly affects mood
                'dopamine': 0.6,
                'cortisol': 0.4,
                'oxytocin': 0.9     # Social connection
            },
            'web_research': {
                'adrenaline': 0.7,  # Learning drives curiosity
                'serotonin': 0.5,
                'dopamine': 0.8,    # Knowledge acquisition is rewarding
                'cortisol': 0.3,
                'oxytocin': 0.2
            },
            'sensor': {
                'adrenaline': 0.8,  # Environmental awareness
                'serotonin': 0.6,
                'dopamine': 0.4,
                'cortisol': 0.7,    # Environmental threats
                'oxytocin': 0.3
            },
            'news': {
                'adrenaline': 0.6,
                'serotonin': 0.4,
                'dopamine': 0.3,
                'cortisol': 0.8,    # News often stressful
                'oxytocin': 0.2
            },
            'performance': {
                'adrenaline': 0.5,
                'serotonin': 0.7,   # Good performance = satisfaction
                'dopamine': 0.6,
                'cortisol': 0.9,    # Performance issues = stress
                'oxytocin': 0.1
            },
            'curiosity': {
                'adrenaline': 0.9,  # Curiosity is pure adrenaline
                'serotonin': 0.5,
                'dopamine': 0.7,    # Discovery satisfaction
                'cortisol': 0.2,
                'oxytocin': 0.3
            }
        }
    
    def start_feeders(self):
        """Start all enabled feeders"""
        logger.info("Starting all enabled feeders...")
        
        for feeder_name, feeder_config in self.config["feeders"].items():
            if feeder_config["enabled"]:
                self._start_feeder(feeder_name)
        
        # Wait for feeders to initialize
        time.sleep(5)
        self._check_feeder_health()
    
    def _start_feeder(self, feeder_name: str):
        """Start a specific feeder"""
        try:
            feeder_config = self.config["feeders"][feeder_name]
            script_path = os.path.join(os.path.dirname(__file__), feeder_config["script"])
            
            if not os.path.exists(script_path):
                logger.error(f"Feeder script not found: {script_path}")
                self.feeders[feeder_name].status = 'error'
                return
            
            # Start feeder process
            logger.info(f"Starting {feeder_name} feeder...")
            
            process = subprocess.Popen(
                [sys.executable, script_path],
                cwd=os.path.dirname(os.path.dirname(script_path)),  # Run from SAIGE root
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Update feeder status
            self.feeders[feeder_name].pid = process.pid
            self.feeders[feeder_name].status = 'starting'
            self.feeders[feeder_name].process_handle = process
            self.feeders[feeder_name].last_update = time.time()
            
            logger.info(f"Started {feeder_name} feeder with PID {process.pid}")
            
        except Exception as e:
            logger.error(f"Error starting {feeder_name} feeder: {e}")
            self.feeders[feeder_name].status = 'error'
            self.feeders[feeder_name].error_count += 1
    
    def _check_feeder_health(self):
        """Check health of all feeders"""
        try:
            for feeder_name, feeder_status in self.feeders.items():
                if not self.config["feeders"][feeder_name]["enabled"]:
                    continue
                
                # Check if process is running
                if feeder_status.pid:
                    try:
                        process = psutil.Process(feeder_status.pid)
                        if process.is_running():
                            feeder_status.status = 'running'
                            
                            # Check stimulus output freshness
                            stimulus_file = f"data/{feeder_name}_stimulus.json"
                            if os.path.exists(stimulus_file):
                                file_age = time.time() - os.path.getmtime(stimulus_file)
                                if file_age < 300:  # Less than 5 minutes old
                                    feeder_status.health_score = 1.0
                                elif file_age < 600:  # Less than 10 minutes old
                                    feeder_status.health_score = 0.7
                                else:
                                    feeder_status.health_score = 0.3
                            else:
                                feeder_status.health_score = 0.5  # No output yet
                        else:
                            feeder_status.status = 'stopped'
                            feeder_status.health_score = 0.0
                    except psutil.NoSuchProcess:
                        feeder_status.status = 'stopped'
                        feeder_status.pid = None
                        feeder_status.health_score = 0.0
                
                # Auto-restart if needed
                if (feeder_status.status in ['stopped', 'error'] and
                    self.config["feeders"][feeder_name]["auto_restart"] and
                    feeder_status.restart_count < self.config["coordination"]["max_restart_attempts"]):
                    
                    logger.warning(f"Auto-restarting {feeder_name} feeder...")
                    feeder_status.restart_count += 1
                    self._start_feeder(feeder_name)
            
        except Exception as e:
            logger.error(f"Error checking feeder health: {e}")
    
    def collect_stimulus_data(self) -> Dict[str, Dict[str, float]]:
        """Collect stimulus data from all active feeders"""
        stimulus_data = {}
        
        for feeder_name, feeder_status in self.feeders.items():
            if feeder_status.status != 'running':
                continue
            
            stimulus_file = f"data/{feeder_name}_stimulus.json"
            
            try:
                if os.path.exists(stimulus_file):
                    with open(stimulus_file, 'r') as f:
                        data = json.load(f)
                    
                    if 'stimulus' in data:
                        stimulus_data[feeder_name] = data['stimulus']
                        feeder_status.last_stimulus = data['stimulus']
                        feeder_status.last_update = time.time()
                    
            except Exception as e:
                logger.debug(f"Error reading stimulus from {feeder_name}: {e}")
                feeder_status.error_count += 1
        
        return stimulus_data
    
    def aggregate_stimulus(self, individual_stimuli: Dict[str, Dict[str, float]]) -> AggregatedStimulus:
        """Aggregate stimulus from multiple feeders into unified hormone levels"""
        
        try:
            timestamp = time.time()
            
            # Initialize aggregated values
            hormones = ['adrenaline', 'serotonin', 'dopamine', 'cortisol', 'oxytocin']
            aggregated = {hormone: 0.0 for hormone in hormones}
            confidence_scores = {hormone: 0.0 for hormone in hormones}
            dominant_sources = {hormone: 'none' for hormone in hormones}
            coordination_notes = []
            
            if not individual_stimuli:
                coordination_notes.append("No active feeders providing stimulus")
                return AggregatedStimulus(
                    timestamp=timestamp,
                    individual_stimuli=individual_stimuli,
                    aggregated_values=aggregated,
                    confidence_scores=confidence_scores,
                    dominant_sources=dominant_sources,
                    coordination_notes=coordination_notes
                )
            
            # Calculate weighted aggregation
            for hormone in hormones:
                weighted_sum = 0.0
                total_weight = 0.0
                max_value = 0.0
                max_source = 'none'
                
                for feeder_name, stimulus in individual_stimuli.items():
                    if hormone in stimulus:
                        # Get feeder configuration
                        feeder_config = self.config["feeders"].get(feeder_name, {})
                        feeder_weight = feeder_config.get("weight", 0.5)
                        feeder_priority = feeder_config.get("priority", 0.5)
                        
                        # Get hormone-specific weight
                        hormone_weight = self.aggregation_weights.get(feeder_name, {}).get(hormone, 0.5)
                        
                        # Calculate final weight
                        final_weight = feeder_weight * feeder_priority * hormone_weight
                        
                        # Get feeder health factor
                        health_factor = self.feeders[feeder_name].health_score
                        final_weight *= health_factor
                        
                        # Add to weighted sum
                        stimulus_value = stimulus[hormone]
                        weighted_sum += stimulus_value * final_weight
                        total_weight += final_weight
                        
                        # Track dominant source
                        if stimulus_value > max_value:
                            max_value = stimulus_value
                            max_source = feeder_name
                
                # Calculate final aggregated value
                if total_weight > 0:
                    aggregated[hormone] = weighted_sum / total_weight
                    confidence_scores[hormone] = min(total_weight / len(individual_stimuli), 1.0)
                    dominant_sources[hormone] = max_source
                else:
                    aggregated[hormone] = 0.0
                    confidence_scores[hormone] = 0.0
            
            # Apply coordination strategy adjustments
            aggregated = self._apply_coordination_strategy(aggregated, individual_stimuli, coordination_notes)
            
            # Normalize values to [0, 1] range
            for hormone in hormones:
                aggregated[hormone] = max(0.0, min(aggregated[hormone], 1.0))
            
            coordination_notes.append(f"Aggregated stimulus from {len(individual_stimuli)} feeders")
            
            return AggregatedStimulus(
                timestamp=timestamp,
                individual_stimuli=individual_stimuli,
                aggregated_values=aggregated,
                confidence_scores=confidence_scores,
                dominant_sources=dominant_sources,
                coordination_notes=coordination_notes
            )
            
        except Exception as e:
            logger.error(f"Error aggregating stimulus: {e}")
            # Return default stimulus on error
            return AggregatedStimulus(
                timestamp=time.time(),
                individual_stimuli=individual_stimuli,
                aggregated_values={h: 0.3 for h in hormones},
                confidence_scores={h: 0.0 for h in hormones},
                dominant_sources={h: 'error' for h in hormones},
                coordination_notes=[f"Error in aggregation: {str(e)}"]
            )
    
    def _apply_coordination_strategy(self, aggregated: Dict[str, float], 
                                   individual_stimuli: Dict[str, Dict[str, float]],
                                   coordination_notes: List[str]) -> Dict[str, float]:
        """Apply coordination strategy to modify aggregated stimulus"""
        
        try:
            strategy = self.config["coordination"]["aggregation_strategy"]
            
            if strategy == "balanced":
                # Ensure no hormone dominates too much
                max_value = max(aggregated.values())
                if max_value > 0.8:
                    scale_factor = 0.8 / max_value
                    for hormone in aggregated:
                        aggregated[hormone] *= scale_factor
                    coordination_notes.append("Applied balanced scaling to prevent hormone dominance")
            
            elif strategy == "adaptive":
                # Adapt based on system state and recent history
                
                # Check recent stimulus history for patterns
                recent_stimuli = list(self.stimulus_history)[-10:]  # Last 10 aggregations
                
                if recent_stimuli:
                    # Calculate average recent values
                    avg_recent = {}
                    for hormone in aggregated.keys():
                        values = [s.aggregated_values.get(hormone, 0) for s in recent_stimuli]
                        avg_recent[hormone] = np.mean(values) if values else 0
                    
                    # Dampen hormones that have been consistently high
                    for hormone, current_value in aggregated.items():
                        if avg_recent[hormone] > 0.7 and current_value > 0.7:
                            damping_factor = 0.8
                            aggregated[hormone] *= damping_factor
                            coordination_notes.append(f"Applied adaptive damping to {hormone}")
                        
                        # Boost hormones that have been consistently low but now have stimulus
                        elif avg_recent[hormone] < 0.3 and current_value > 0.5:
                            boost_factor = 1.2
                            aggregated[hormone] = min(aggregated[hormone] * boost_factor, 1.0)
                            coordination_notes.append(f"Applied adaptive boost to {hormone}")
                
                # Consider system performance
                performance_feeder = individual_stimuli.get('performance', {})
                if performance_feeder.get('cortisol', 0) > 0.7:
                    # High system stress - reduce other stress sources
                    aggregated['cortisol'] = min(aggregated['cortisol'] * 1.2, 1.0)
                    aggregated['adrenaline'] *= 0.8
                    coordination_notes.append("Adapted for high system stress")
                
                # Consider social context
                conversation_feeder = individual_stimuli.get('conversation', {})
                if conversation_feeder.get('oxytocin', 0) > 0.6:
                    # Strong social context - enhance social hormones
                    aggregated['oxytocin'] = min(aggregated['oxytocin'] * 1.1, 1.0)
                    aggregated['serotonin'] = min(aggregated['serotonin'] * 1.1, 1.0)
                    coordination_notes.append("Enhanced social hormone response")
            
            return aggregated
            
        except Exception as e:
            logger.error(f"Error applying coordination strategy: {e}")
            return aggregated
    
    def integrate_with_brain(self, aggregated_stimulus: AggregatedStimulus):
        """Integrate aggregated stimulus with SAIGE's brain system"""
        try:
            integration_success = self.brain_interface.process_stimulus(aggregated_stimulus)
            
            if integration_success:
                self.coordination_metrics['brain_integration_success_rate'] = (
                    self.coordination_metrics['brain_integration_success_rate'] * 0.9 + 0.1
                )
                logger.debug("Successfully integrated stimulus with brain system")
            else:
                self.coordination_metrics['brain_integration_success_rate'] *= 0.9
                logger.warning("Failed to integrate stimulus with brain system")
            
        except Exception as e:
            logger.error(f"Error integrating with brain system: {e}")
            self.coordination_metrics['brain_integration_success_rate'] *= 0.9
    
    def generate_learning_directives(self, aggregated_stimulus: AggregatedStimulus) -> List[LearningDirective]:
        """Generate learning directives based on current state"""
        directives = []
        
        try:
            current_time = time.time()
            stimulus_values = aggregated_stimulus.aggregated_values
            
            # High curiosity (adrenaline) suggests exploration needed
            if stimulus_values.get('adrenaline', 0) > 0.7:
                directive = LearningDirective(
                    directive_id=f"explore_{int(current_time)}",
                    priority=0.8,
                    directive_type='exploration',
                    description="High curiosity detected - initiate exploration activities",
                    target_feeders=['curiosity', 'web_research'],
                    expected_duration=2.0,  # 2 hours
                    success_criteria={'new_knowledge_acquired': True, 'curiosity_satisfied': True},
                    created_timestamp=current_time,
                    status='pending'
                )
                directives.append(directive)
            
            # High stress (cortisol) suggests need for optimization
            if stimulus_values.get('cortisol', 0) > 0.6:
                directive = LearningDirective(
                    directive_id=f"optimize_{int(current_time)}",
                    priority=0.9,
                    directive_type='optimization',
                    description="High stress detected - optimize system performance",
                    target_feeders=['performance', 'curiosity'],
                    expected_duration=1.0,  # 1 hour
                    success_criteria={'stress_reduced': True, 'performance_improved': True},
                    created_timestamp=current_time,
                    status='pending'
                )
                directives.append(directive)
            
            # High satisfaction suggests consolidation opportunity
            if stimulus_values.get('dopamine', 0) > 0.6 and stimulus_values.get('serotonin', 0) > 0.6:
                directive = LearningDirective(
                    directive_id=f"consolidate_{int(current_time)}",
                    priority=0.6,
                    directive_type='consolidation',
                    description="High satisfaction - consolidate recent learning",
                    target_feeders=['conversation', 'web_research'],
                    expected_duration=0.5,  # 30 minutes
                    success_criteria={'knowledge_consolidated': True, 'memory_organized': True},
                    created_timestamp=current_time,
                    status='pending'
                )
                directives.append(directive)
            
            # Social connection opportunities
            if stimulus_values.get('oxytocin', 0) > 0.5:
                directive = LearningDirective(
                    directive_id=f"social_{int(current_time)}",
                    priority=0.7,
                    directive_type='adaptation',
                    description="Social connection opportunity - enhance interaction learning",
                    target_feeders=['conversation', 'news'],
                    expected_duration=1.5,  # 1.5 hours
                    success_criteria={'social_learning_enhanced': True, 'interaction_improved': True},
                    created_timestamp=current_time,
                    status='pending'
                )
                directives.append(directive)
            
            # Update active directives
            for directive in directives:
                self.active_directives[directive.directive_id] = directive
            
            # Clean up old directives
            self._cleanup_old_directives()
            
            return directives
            
        except Exception as e:
            logger.error(f"Error generating learning directives: {e}")
            return []
    
    def _cleanup_old_directives(self):
        """Remove old or completed learning directives"""
        try:
            current_time = time.time()
            max_age = 86400  # 24 hours
            
            expired_directives = []
            for directive_id, directive in self.active_directives.items():
                age = current_time - directive.created_timestamp
                if age > max_age or directive.status in ['completed', 'failed']:
                    expired_directives.append(directive_id)
            
            for directive_id in expired_directives:
                del self.active_directives[directive_id]
            
        except Exception as e:
            logger.error(f"Error cleaning up directives: {e}")
    
    def save_coordination_data(self, aggregated_stimulus: AggregatedStimulus, 
                              learning_directives: List[LearningDirective]):
        """Save coordination data and status"""
        try:
            # Save aggregated stimulus
            stimulus_data = asdict(aggregated_stimulus)
            
            os.makedirs(os.path.dirname(self.config["output"]["aggregated_stimulus"]), exist_ok=True)
            with open(self.config["output"]["aggregated_stimulus"], 'w') as f:
                json.dump(stimulus_data, f, indent=2)
            
            # Save coordination status
            coordination_status = {
                'timestamp': time.time(),
                'feeders': {name: {
                    'status': feeder.status,
                    'health_score': feeder.health_score,
                    'error_count': feeder.error_count,
                    'restart_count': feeder.restart_count,
                    'last_update': feeder.last_update
                } for name, feeder in self.feeders.items()},
                'metrics': self.coordination_metrics,
                'active_directives_count': len(self.active_directives)
            }
            
            with open(self.config["output"]["coordination_status"], 'w') as f:
                json.dump(coordination_status, f, indent=2)
            
            # Save learning directives
            directives_data = {
                'active_directives': [asdict(directive) for directive in learning_directives],
                'all_active_directives': [asdict(directive) for directive in self.active_directives.values()],
                'last_updated': time.time()
            }
            
            with open(self.config["output"]["learning_directives"], 'w') as f:
                json.dump(directives_data, f, indent=2)
            
            # Save system health
            overall_health = np.mean([f.health_score for f in self.feeders.values() if f.health_score > 0])
            system_health = {
                'timestamp': time.time(),
                'overall_health': overall_health,
                'individual_feeder_health': {name: f.health_score for name, f in self.feeders.items()},
                'coordination_success_rate': self.coordination_metrics.get('successful_aggregations', 0) / max(self.coordination_metrics.get('total_cycles', 1), 1),
                'brain_integration_success_rate': self.coordination_metrics['brain_integration_success_rate']
            }
            
            with open(self.config["output"]["system_health"], 'w') as f:
                json.dump(system_health, f, indent=2)
            
            logger.debug("Coordination data saved successfully")
            
        except Exception as e:
            logger.error(f"Error saving coordination data: {e}")
    
    def run_coordination_cycle(self):
        """Run one complete coordination cycle"""
        try:
            cycle_start = time.time()
            
            # Update metrics
            self.coordination_metrics['total_cycles'] += 1
            
            # Check feeder health
            self._check_feeder_health()
            
            # Collect stimulus data
            individual_stimuli = self.collect_stimulus_data()
            
            # Aggregate stimulus
            aggregated_stimulus = self.aggregate_stimulus(individual_stimuli)
            
            # Store in history
            self.stimulus_history.append(aggregated_stimulus)
            
            # Integrate with brain system
            self.integrate_with_brain(aggregated_stimulus)
            
            # Generate learning directives
            learning_directives = self.generate_learning_directives(aggregated_stimulus)
            
            # Save coordination data
            self.save_coordination_data(aggregated_stimulus, learning_directives)
            
            # Update success metrics
            if aggregated_stimulus.aggregated_values:
                self.coordination_metrics['successful_aggregations'] += 1
            else:
                self.coordination_metrics['failed_coordinations'] += 1
            
            # Update response time
            cycle_time = time.time() - cycle_start
            self.coordination_metrics['average_response_time'] = (
                self.coordination_metrics['average_response_time'] * 0.9 + cycle_time * 0.1
            )
            
            logger.info(f"Coordination cycle completed in {cycle_time:.2f}s")
            logger.info(f"Aggregated stimulus: {aggregated_stimulus.aggregated_values}")
            if learning_directives:
                logger.info(f"Generated {len(learning_directives)} learning directives")
            
        except Exception as e:
            logger.error(f"Error in coordination cycle: {e}")
            self.coordination_metrics['failed_coordinations'] += 1
    
    def run_continuous_coordination(self):
        """Run continuous coordination of all feeders"""
        logger.info("Starting continuous feeder coordination...")
        
        # Start all feeders
        self.start_feeders()
        
        # Main coordination loop
        while not self.shutdown_requested:
            try:
                self.run_coordination_cycle()
                
                # Sleep until next cycle
                time.sleep(self.config["coordination"]["stimulus_aggregation_interval"])
                
            except KeyboardInterrupt:
                logger.info("Coordination interrupted by user")
                break
            except Exception as e:
                logger.error(f"Error in coordination loop: {e}")
                time.sleep(10)  # Brief pause before retry
        
        # Shutdown cleanup
        self._shutdown_all_feeders()
    
    def _shutdown_all_feeders(self):
        """Shutdown all feeder processes"""
        logger.info("Shutting down all feeders...")
        
        for feeder_name, feeder_status in self.feeders.items():
            if feeder_status.process_handle:
                try:
                    feeder_status.process_handle.terminate()
                    feeder_status.process_handle.wait(timeout=10)
                    logger.info(f"Shutdown {feeder_name} feeder")
                except Exception as e:
                    logger.warning(f"Error shutting down {feeder_name}: {e}")
                    try:
                        feeder_status.process_handle.kill()
                    except:
                        pass
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info(f"Received signal {signum}, shutting down gracefully...")
        self.shutdown_requested = True


class BrainInterface:
    """Interface for integrating with SAIGE's brain system"""
    
    def __init__(self, config: Dict):
        self.config = config
        self.working_memory_file = config.get('brain_memory_file', 'brain/working_memory.json')
        self.long_term_memory_file = config.get('long_term_memory_file', 'brain/long_term_memory.json')
    
    def process_stimulus(self, aggregated_stimulus: AggregatedStimulus) -> bool:
        """Process aggregated stimulus and update brain state"""
        try:
            # Update working memory with current stimulus
            self._update_working_memory(aggregated_stimulus)
            
            # Check if consolidation is needed
            if self._should_consolidate(aggregated_stimulus):
                self._consolidate_to_long_term_memory(aggregated_stimulus)
            
            return True
            
        except Exception as e:
            logger.error(f"Error processing stimulus in brain interface: {e}")
            return False
    
    def _update_working_memory(self, stimulus: AggregatedStimulus):
        """Update working memory with current stimulus"""
        try:
            # Load existing working memory
            working_memory = {}
            if os.path.exists(self.working_memory_file):
                with open(self.working_memory_file, 'r') as f:
                    working_memory = json.load(f)
            
            # Add current stimulus
            working_memory['current_stimulus'] = asdict(stimulus)
            working_memory['last_updated'] = time.time()
            
            # Maintain stimulus history in working memory
            if 'stimulus_history' not in working_memory:
                working_memory['stimulus_history'] = []
            
            working_memory['stimulus_history'].append({
                'timestamp': stimulus.timestamp,
                'values': stimulus.aggregated_values,
                'dominant_sources': stimulus.dominant_sources
            })
            
            # Keep only recent history (last 50 entries)
            working_memory['stimulus_history'] = working_memory['stimulus_history'][-50:]
            
            # Save updated working memory
            os.makedirs(os.path.dirname(self.working_memory_file), exist_ok=True)
            with open(self.working_memory_file, 'w') as f:
                json.dump(working_memory, f, indent=2)
            
        except Exception as e:
            logger.error(f"Error updating working memory: {e}")
    
    def _should_consolidate(self, stimulus: AggregatedStimulus) -> bool:
        """Determine if consolidation to long-term memory is needed"""
        try:
            threshold = self.config.get('consolidation_trigger_threshold', 0.7)
            
            # Consolidate if any hormone value is above threshold
            max_stimulus = max(stimulus.aggregated_values.values()) if stimulus.aggregated_values else 0
            
            return max_stimulus > threshold
            
        except Exception as e:
            logger.error(f"Error checking consolidation criteria: {e}")
            return False
    
    def _consolidate_to_long_term_memory(self, stimulus: AggregatedStimulus):
        """Consolidate significant experiences to long-term memory"""
        try:
            # Load existing long-term memory
            long_term_memory = {}
            if os.path.exists(self.long_term_memory_file):
                with open(self.long_term_memory_file, 'r') as f:
                    long_term_memory = json.load(f)
            
            # Initialize structure
            if 'significant_experiences' not in long_term_memory:
                long_term_memory['significant_experiences'] = []
            
            # Create consolidated experience entry
            experience = {
                'timestamp': stimulus.timestamp,
                'stimulus_values': stimulus.aggregated_values,
                'dominant_sources': stimulus.dominant_sources,
                'coordination_notes': stimulus.coordination_notes,
                'significance_score': max(stimulus.aggregated_values.values()) if stimulus.aggregated_values else 0,
                'consolidated_at': time.time()
            }
            
            long_term_memory['significant_experiences'].append(experience)
            
            # Keep only most significant experiences (limit to 1000)
            long_term_memory['significant_experiences'].sort(
                key=lambda x: x['significance_score'], reverse=True
            )
            long_term_memory['significant_experiences'] = long_term_memory['significant_experiences'][:1000]
            
            # Save updated long-term memory
            os.makedirs(os.path.dirname(self.long_term_memory_file), exist_ok=True)
            with open(self.long_term_memory_file, 'w') as f:
                json.dump(long_term_memory, f, indent=2)
            
            logger.debug("Consolidated significant experience to long-term memory")
            
        except Exception as e:
            logger.error(f"Error consolidating to long-term memory: {e}")


def main():
    """Main entry point"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    coordinator = FeederCoordinator()
    
    # Test mode: run single coordination cycle
    if len(sys.argv) > 1 and sys.argv[1] == '--test':
        coordinator.start_feeders()
        time.sleep(10)  # Wait for feeders to produce data
        coordinator.run_coordination_cycle()
        coordinator._shutdown_all_feeders()
    else:
        # Continuous coordination mode
        coordinator.run_continuous_coordination()


if __name__ == "__main__":
    main()