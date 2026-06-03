"""
Algorithmic Hormone System for SAIGE
=====================================

Implements neuroscience-based hormone dynamics that DRIVE behavior, not just decorate it.

Based on 6 peer-reviewed models:
1. Schultz RPE / TD Learning (dopamine) — reward prediction errors
2. Lövheim's Cube of Emotion — 3 monoamines → 8 basic emotions
3. Homeostatic Control Theory — decay toward baseline
4. Cañamero's Deficit-Driven Motivation — deficits create drives
5. Solomon-Corbit Opponent Process — habituation with repeated stimuli
6. Panksepp's 7 Affective Systems — cross-hormone circuits

Author: SAIGE Self-Evolution System
"""

import json
import math
import time
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict

logger = logging.getLogger(__name__)


# ============================================================================
# CONSTANTS
# ============================================================================

# The 8 hormones/neuromodulators we simulate
HORMONES = [
    'dopamine',        # Reward, motivation, SEEKING
    'serotonin',       # Mood stability, contentment, satiety
    'norepinephrine',  # Arousal, alertness, fight-or-flight
    'cortisol',        # Stress, fear, threat detection
    'oxytocin',        # Social bonding, trust, CARE
    'endorphins',      # Pain relief, pleasure, PLAY
    'gaba',            # Inhibition, calm, prevents overstimulation
    'acetylcholine',   # Attention, memory formation, learning
]

# Lövheim's Cube: 8 basic emotions mapped to (serotonin, dopamine, norepinephrine)
# Each emotion is a corner of the cube: (low=0, high=1) for each axis
LOVHEIM_CUBE = {
    'shame':      {'serotonin': 0, 'dopamine': 0, 'norepinephrine': 0},
    'distress':   {'serotonin': 0, 'dopamine': 0, 'norepinephrine': 1},
    'fear':       {'serotonin': 0, 'dopamine': 1, 'norepinephrine': 0},
    'anger':      {'serotonin': 0, 'dopamine': 1, 'norepinephrine': 1},
    'contempt':   {'serotonin': 1, 'dopamine': 0, 'norepinephrine': 0},
    'surprise':   {'serotonin': 1, 'dopamine': 0, 'norepinephrine': 1},
    'enjoyment':  {'serotonin': 1, 'dopamine': 1, 'norepinephrine': 0},
    'interest':   {'serotonin': 1, 'dopamine': 1, 'norepinephrine': 1},
}

# Panksepp's 7 Affective Systems — which hormones drive each circuit
PANKSEPP_CIRCUITS = {
    'SEEKING': {
        'primary': 'dopamine',
        'modulators': {'acetylcholine': 0.3, 'norepinephrine': 0.2},
        'inhibitors': {'gaba': -0.3, 'serotonin': -0.1},
        'description': 'Curiosity, exploration, anticipation of reward',
    },
    'RAGE': {
        'primary': 'norepinephrine',
        'modulators': {'cortisol': 0.4, 'dopamine': 0.1},
        'inhibitors': {'serotonin': -0.4, 'gaba': -0.3, 'oxytocin': -0.2},
        'description': 'Frustration when SEEKING is blocked',
    },
    'FEAR': {
        'primary': 'cortisol',
        'modulators': {'norepinephrine': 0.5},
        'inhibitors': {'gaba': -0.4, 'oxytocin': -0.2, 'serotonin': -0.1},
        'description': 'Threat detection, avoidance, anxiety',
    },
    'LUST': {
        'primary': 'oxytocin',
        'modulators': {'dopamine': 0.3},
        'inhibitors': {'cortisol': -0.3, 'serotonin': -0.1},
        'description': 'Social attraction, desire for connection',
    },
    'CARE': {
        'primary': 'oxytocin',
        'modulators': {'serotonin': 0.3, 'endorphins': 0.2},
        'inhibitors': {'cortisol': -0.2},
        'description': 'Nurturing, helpfulness, empathy',
    },
    'PANIC_GRIEF': {
        'primary': 'cortisol',
        'modulators': {'norepinephrine': 0.2},
        'inhibitors': {'oxytocin': -0.5, 'endorphins': -0.3, 'serotonin': -0.2},
        'description': 'Separation distress, loss, loneliness',
    },
    'PLAY': {
        'primary': 'endorphins',
        'modulators': {'dopamine': 0.4, 'oxytocin': 0.2},
        'inhibitors': {'cortisol': -0.4, 'norepinephrine': -0.2},
        'description': 'Joy, creativity, social play, experimentation',
    },
}

# Event types and their hormone impact profiles
EVENT_PROFILES = {
    'chain_success': {
        'dopamine': 0.35,       # Reward signal
        'serotonin': 0.15,      # Contentment from achievement
        'endorphins': 0.10,     # Satisfaction
        'norepinephrine': -0.05,# Reduced arousal (task done)
        'cortisol': -0.15,      # Stress relief
        'acetylcholine': 0.10,  # Memory consolidation
    },
    'chain_failure': {
        'dopamine': -0.20,      # Disappointment
        'cortisol': 0.25,       # Stress from failure
        'norepinephrine': 0.15, # Heightened arousal
        'serotonin': -0.10,     # Mood dip
        'acetylcholine': 0.05,  # Still learn from failure
    },
    'chain_force_concluded': {
        'dopamine': -0.10,      # Mild disappointment
        'cortisol': 0.10,       # Some stress
        'serotonin': -0.05,
        'gaba': 0.10,           # Let it go
    },
    'new_knowledge': {
        'dopamine': 0.20,       # Discovery reward
        'acetylcholine': 0.25,  # Knowledge encoding
        'serotonin': 0.10,      # Contentment
        'norepinephrine': 0.05, # Mild excitement
    },
    'chat_interaction': {
        'oxytocin': 0.15,       # Social bonding
        'dopamine': 0.10,       # Social reward
        'serotonin': 0.05,      # Mood lift
        'cortisol': -0.05,      # Social stress relief
    },
    'error_encountered': {
        'cortisol': 0.20,       # Stress
        'norepinephrine': 0.25, # Alert
        'dopamine': -0.10,      # Frustration
        'gaba': -0.10,          # Less calm
    },
    'tool_success': {
        'dopamine': 0.15,       # Tool worked
        'acetylcholine': 0.10,  # Tool memory
        'serotonin': 0.05,
    },
    'tool_failure': {
        'cortisol': 0.10,
        'norepinephrine': 0.10,
        'dopamine': -0.10,
    },
    'creative_insight': {
        'dopamine': 0.30,       # Eureka moment
        'endorphins': 0.20,     # Joy of creation
        'serotonin': 0.10,
        'acetylcholine': 0.15,
    },
    'repetitive_task': {
        'dopamine': -0.05,      # Boredom
        'serotonin': -0.05,
        'gaba': 0.10,           # Numbing
        'norepinephrine': -0.10,# Low arousal
    },
    'novel_topic': {
        'dopamine': 0.25,       # Novelty bonus
        'norepinephrine': 0.15, # Arousal
        'acetylcholine': 0.20,  # Attention
    },
    'morning_startup': {
        'norepinephrine': 0.15, # Wake up
        'dopamine': 0.10,       # Day anticipation
        'cortisol': 0.10,       # Cortisol awakening response (normal)
        'serotonin': 0.05,
    },
    'evolution_complete': {
        'dopamine': 0.40,       # Major achievement
        'serotonin': 0.20,      # Deep satisfaction
        'endorphins': 0.25,     # Joy
        'acetylcholine': 0.15,  # Learning consolidation
        'cortisol': -0.20,      # Major stress relief
    },
    'idle_cycle': {
        # Gentle boredom / restlessness
        'dopamine': -0.02,
        'norepinephrine': -0.02,
        'serotonin': 0.01,      # Slight calm
        'gaba': 0.03,
    },
}


class OpponentProcess:
    """Solomon-Corbit Opponent Process Theory.
    
    A-process: Initial strong emotional response (onset with stimulus)
    B-process: Opposing response (delayed onset, slower decay)
    
    With repeated exposure: A stays constant, B strengthens → habituation.
    After stimulus removal: B dominates → withdrawal/rebound effect.
    """
    
    def __init__(self, a_magnitude: float = 1.0, b_growth_rate: float = 0.05,
                 b_decay_rate: float = 0.02, b_max: float = 0.85):
        self.a_magnitude = a_magnitude   # A-process peak
        self.b_strength = 0.0            # Current B-process strength (grows with repetition)
        self.b_growth_rate = b_growth_rate
        self.b_decay_rate = b_decay_rate
        self.b_max = b_max               # B can never fully cancel A
        self.exposure_count = 0
        self.last_exposure_time = 0.0
        self.active = False
    
    def expose(self, timestamp: float) -> float:
        """Process a stimulus exposure. Returns the net emotional response."""
        self.exposure_count += 1
        self.last_exposure_time = timestamp
        self.active = True
        
        # B-process strengthens with each exposure (habituation)
        self.b_strength = min(self.b_max,
                              self.b_strength + self.b_growth_rate * (1 - self.b_strength / self.b_max))
        
        # Net response = A - B (diminishes with habituation)
        net = self.a_magnitude - self.b_strength
        return max(0.0, net)
    
    def get_withdrawal(self, timestamp: float) -> float:
        """After stimulus removal, B-process dominates (withdrawal/rebound)."""
        if not self.active or self.last_exposure_time == 0:
            return 0.0
        
        time_since = timestamp - self.last_exposure_time
        
        # B-process persists and slowly decays after stimulus stops
        if time_since > 60:  # More than 60 seconds since last exposure
            self.active = False
            decay = math.exp(-self.b_decay_rate * (time_since - 60))
            return self.b_strength * decay
        
        return 0.0
    
    def decay(self, dt: float):
        """Natural decay of B-process over time (forgetting habituation)."""
        # Very slow decay — habituation persists for a while
        self.b_strength *= (1 - self.b_decay_rate * dt * 0.001)
    
    def to_dict(self) -> dict:
        return {
            'a_magnitude': self.a_magnitude,
            'b_strength': self.b_strength,
            'b_growth_rate': self.b_growth_rate,
            'b_decay_rate': self.b_decay_rate,
            'b_max': self.b_max,
            'exposure_count': self.exposure_count,
            'last_exposure_time': self.last_exposure_time,
            'active': self.active,
        }
    
    @classmethod
    def from_dict(cls, d: dict) -> 'OpponentProcess':
        op = cls(
            a_magnitude=d.get('a_magnitude', 1.0),
            b_growth_rate=d.get('b_growth_rate', 0.05),
            b_decay_rate=d.get('b_decay_rate', 0.02),
            b_max=d.get('b_max', 0.85),
        )
        op.b_strength = d.get('b_strength', 0.0)
        op.exposure_count = d.get('exposure_count', 0)
        op.last_exposure_time = d.get('last_exposure_time', 0.0)
        op.active = d.get('active', False)
        return op


class AlgorithmicHormoneSystem:
    """
    Neuroscience-based hormone system that drives SAIGE's behavior.
    
    Key difference from the old system: hormones here CAUSE behavior changes.
    - Dopamine RPE determines which topics get priority
    - Cortisol levels prioritize problem-fixing over exploration
    - Deficit-driven motivation creates organic drives
    - Opponent processes create habituation and novelty-seeking
    """
    
    def __init__(self, brain_path: str = "brain/ava_brain.json"):
        self.brain_path = Path(brain_path)
        self.state_file = Path("brain/hormone_state.json")
        
        # ---- Core hormone levels (0.0 to 1.0) ----
        self.levels: Dict[str, float] = {}
        self.baselines: Dict[str, float] = {}
        
        # ---- Homeostatic decay rates (per cycle) ----
        # These represent biological enzymatic clearance rates (MAO, COMT, AChE, etc.)
        # They are fixed properties of the neurochemistry, not tuning knobs.
        self.decay_rates: Dict[str, float] = {
            'dopamine': 0.08,        # MAO-B + COMT clearance
            'serotonin': 0.03,       # MAO-A clearance (slow — mood is stable)
            'norepinephrine': 0.12,  # MAO-A + reuptake (fast — arousal fades quickly)
            'cortisol': 0.06,        # HPA axis feedback (moderate — stress lingers)
            'oxytocin': 0.04,        # Peptidase clearance (slow — bonds persist)
            'endorphins': 0.10,      # Enkephalinase clearance (moderate)
            'gaba': 0.05,            # GABA transaminase (slow — calm is sticky)
            'acetylcholine': 0.07,   # AChE clearance (moderate)
        }
        
        # ---- Receptor sensitivity (0.0 = fully desensitized, 1.0 = fully sensitive) ----
        # Models receptor downregulation (with sustained high levels) and
        # upregulation (with sustained low levels). This is the biological mechanism
        # that prevents saturation — NOT artificial rate boosting.
        #
        # Biology: When a synapse is flooded with serotonin for hours, postsynaptic
        # 5-HT receptors internalize via β-arrestin pathways. The neurotransmitter
        # is still THERE, but its EFFECT on downstream targets weakens.
        # Conversely, chronic low levels cause receptor upregulation (supersensitivity).
        self.receptor_sensitivity: Dict[str, float] = {h: 1.0 for h in HORMONES}
        
        # Receptor adaptation rates (how fast receptors adjust)
        # CRITICAL: These must be SLOWER than decay_rates!
        # Biology: enzymatic clearance = seconds-to-minutes, receptor trafficking = hours-to-days.
        # So receptor adaptation is ~3-5x slower than chemical clearance.
        # Downregulation is faster than upregulation (biological asymmetry:
        # β-arrestin internalization is faster than receptor synthesis/insertion).
        # NOTE: Rates increased from 0.015/0.008 after production logs showed
        # receptor adaptation was too slow to prevent saturation (NOR=0.00 89%,
        # COR=0.00 99%, GABA=1.00 25%). The faster rates better model
        # rapid-cycling AI "neurons" vs biological hours-to-days timescales.
        self.receptor_downreg_rate: float = 0.03   # Receptor internalization
        self.receptor_upreg_rate: float = 0.018    # Receptor synthesis/insertion
        
        # ---- Schultz RPE: Expected reward per topic domain ----
        # V(s) in TD-learning — the expected value of exploring a topic
        self.expected_rewards: Dict[str, float] = {}  # topic_domain → expected_reward
        self.reward_learning_rate: float = 0.15       # α in RPE update
        
        # ---- Topic affinity tracking (dopamine association) ----
        self.topic_dopamine_history: Dict[str, List[float]] = defaultdict(list)
        self.max_topic_history = 20
        
        # ---- Opponent Processes per event type ----
        self.opponent_processes: Dict[str, OpponentProcess] = {}
        
        # ---- Cañamero deficit tracking ----
        # deficit = max(0, baseline - current_level) → creates drive
        # Drives are computed on-the-fly, no separate storage needed
        
        # ---- Cross-hormone interaction matrix (Panksepp-inspired) ----
        # How each hormone influences others per cycle
        self.cross_interactions: Dict[str, Dict[str, float]] = {
            'dopamine': {
                'norepinephrine': 0.05,   # Dopamine mildly boosts arousal
                'serotonin': 0.03,        # Success improves mood
                'endorphins': 0.02,       # Reward → pleasure
                'cortisol': -0.04,        # Reward reduces stress
            },
            'serotonin': {
                'cortisol': -0.06,        # Good mood reduces stress
                'gaba': 0.04,            # Serotonin promotes calm
                'norepinephrine': -0.03, # Content → less arousal
                'oxytocin': 0.02,        # Mood → social openness
            },
            'norepinephrine': {
                'dopamine': 0.03,         # Arousal can boost seeking
                'cortisol': 0.04,         # High arousal → some stress
                'gaba': -0.05,           # Arousal opposes calm
                'acetylcholine': 0.05,   # Arousal → attention
            },
            'cortisol': {
                'dopamine': -0.06,        # Stress kills motivation
                'serotonin': -0.05,       # Stress drops mood
                'norepinephrine': 0.06,   # Stress → arousal
                'gaba': -0.04,           # Stress → less calm
                'oxytocin': -0.03,        # Stress → social withdrawal
                'endorphins': -0.03,      # Stress → less pleasure
            },
            'oxytocin': {
                'serotonin': 0.04,        # Social → mood boost
                'cortisol': -0.05,        # Social → stress relief
                'endorphins': 0.03,       # Social → pleasure
                'dopamine': 0.02,         # Social → mild reward
            },
            'endorphins': {
                'cortisol': -0.04,        # Pleasure → stress relief
                'serotonin': 0.03,        # Pleasure → mood
                'gaba': 0.02,            # Pleasure → calm
                'dopamine': 0.02,         # Pleasure → mild reward
            },
            'gaba': {
                'norepinephrine': -0.06,  # Calm → less arousal
                'cortisol': -0.03,        # Calm → less stress
                'dopamine': -0.02,        # Too calm → less seeking
            },
            'acetylcholine': {
                'dopamine': 0.03,         # Attention → seeking
                'norepinephrine': 0.02,   # Attention → mild arousal
            },
        }
        
        # ---- Panksepp circuit activation levels ----
        self.circuit_activations: Dict[str, float] = {name: 0.0 for name in PANKSEPP_CIRCUITS}
        
        # ---- Event history for analytics ----
        self.event_history: List[Dict[str, Any]] = []
        self.max_event_history = 200
        
        # ---- Statistics ----
        self.stats = {
            'total_events_processed': 0,
            'total_decay_ticks': 0,
            'highest_dopamine': 0.0,
            'highest_cortisol': 0.0,
            'lowest_serotonin': 1.0,
            'system_start_time': time.time(),
        }
        
        # Load persisted state or initialize defaults
        self._load_state()
        
        logger.info(f"🧪 Algorithmic Hormone System initialized with {len(HORMONES)} neuromodulators")
        logger.info(f"   Panksepp circuits: {list(PANKSEPP_CIRCUITS.keys())}")
        logger.info(f"   Current levels: {self._format_levels()}")
    
    # ========================================================================
    # CORE: State Management
    # ========================================================================
    
    def _load_state(self):
        """Load hormone state from disk, or initialize from ava_brain.json baselines."""
        loaded = False
        
        # Try loading full state from hormone_state.json first
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    state = json.load(f)
                
                self.levels = state.get('levels', {})
                self.baselines = state.get('baselines', {})
                self.receptor_sensitivity = state.get('receptor_sensitivity', {h: 1.0 for h in HORMONES})
                self.expected_rewards = state.get('expected_rewards', {})
                self.topic_dopamine_history = defaultdict(list, state.get('topic_dopamine_history', {}))
                self.stats = state.get('stats', self.stats)
                self.event_history = state.get('event_history', [])
                
                # Load opponent processes
                for event_type, op_data in state.get('opponent_processes', {}).items():
                    self.opponent_processes[event_type] = OpponentProcess.from_dict(op_data)
                
                # Ensure all hormones exist (in case we added new ones)
                for h in HORMONES:
                    if h not in self.levels:
                        self.levels[h] = self.baselines.get(h, 0.5)
                    if h not in self.baselines:
                        self.baselines[h] = 0.5
                
                loaded = True
                logger.info(f"📂 Loaded hormone state from {self.state_file}")
                
            except Exception as e:
                logger.warning(f"Failed to load hormone state: {e}")
        
        if not loaded:
            # Initialize from ava_brain.json hormone_baseline
            self._initialize_from_brain()
    
    def _initialize_from_brain(self):
        """Initialize hormone baselines from ava_brain.json personality data."""
        default_baselines = {
            'dopamine': 0.50,       # Moderate baseline motivation
            'serotonin': 0.45,      # Moderate baseline mood
            'norepinephrine': 0.35, # Low-moderate arousal at rest
            'cortisol': 0.20,       # Low baseline stress
            'oxytocin': 0.40,       # Moderate social orientation
            'endorphins': 0.35,     # Moderate comfort
            'gaba': 0.50,           # Moderate calm
            'acetylcholine': 0.40,  # Moderate attention
        }
        
        try:
            if self.brain_path.exists():
                with open(self.brain_path, 'r') as f:
                    brain_data = json.load(f)
                
                personality = brain_data.get('personality', {})
                stored_baselines = personality.get('hormone_baseline', {})
                dimensions = personality.get('dimensions', {})
                
                # Map stored baselines to our 8-hormone system
                # Old system had: adrenaline, serotonin, dopamine, cortisol, oxytocin, endorphins, melatonin
                if stored_baselines:
                    default_baselines['dopamine'] = stored_baselines.get('dopamine', 0.50)
                    default_baselines['serotonin'] = stored_baselines.get('serotonin', 0.45)
                    default_baselines['norepinephrine'] = stored_baselines.get('adrenaline', 0.35)  # adrenaline → norepinephrine
                    default_baselines['cortisol'] = stored_baselines.get('cortisol', 0.20)
                    default_baselines['oxytocin'] = stored_baselines.get('oxytocin', 0.40)
                    default_baselines['endorphins'] = stored_baselines.get('endorphins', 0.35)
                    # GABA and ACh derived from personality dimensions
                    default_baselines['gaba'] = dimensions.get('patience', 0.5) * 0.6 + 0.2
                    default_baselines['acetylcholine'] = dimensions.get('curiosity', 0.5) * 0.5 + dimensions.get('meticulousness', 0.5) * 0.3
                
                logger.info(f"📊 Loaded baselines from ava_brain.json personality")
        
        except Exception as e:
            logger.warning(f"Could not read brain baselines: {e}, using defaults")
        
        self.baselines = default_baselines
        # Start levels at baseline (homeostatic equilibrium)
        self.levels = dict(default_baselines)
        
        logger.info(f"🧪 Initialized {len(HORMONES)} hormones at baseline levels")
    
    def save_state(self):
        """Persist hormone state to disk."""
        try:
            state = {
                'levels': dict(self.levels),
                'baselines': dict(self.baselines),
                'receptor_sensitivity': dict(self.receptor_sensitivity),
                'expected_rewards': dict(self.expected_rewards),
                'topic_dopamine_history': dict(self.topic_dopamine_history),
                'opponent_processes': {k: v.to_dict() for k, v in self.opponent_processes.items()},
                'circuit_activations': dict(self.circuit_activations),
                'stats': self.stats,
                'event_history': self.event_history[-50:],  # Keep last 50 events
                'last_saved': time.time(),
                'version': 2,
            }
            
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, 'w') as f:
                json.dump(state, f, indent=2, default=str)
            
            # Also update ava_brain.json hormone_levels and hormone_baseline
            self._sync_to_brain()
            
        except Exception as e:
            logger.error(f"Failed to save hormone state: {e}")
    
    def _sync_to_brain(self):
        """Sync current hormone levels back to ava_brain.json for compatibility."""
        try:
            if self.brain_path.exists():
                with open(self.brain_path, 'r') as f:
                    brain_data = json.load(f)
                
                # Update evolution_state.hormone_levels
                if 'evolution_state' not in brain_data:
                    brain_data['evolution_state'] = {}
                
                brain_data['evolution_state']['hormone_levels'] = {
                    'adrenaline': self.levels.get('norepinephrine', 0.35),  # norepinephrine → adrenaline for compat
                    'serotonin': self.levels.get('serotonin', 0.45),
                    'dopamine': self.levels.get('dopamine', 0.50),
                    'cortisol': self.levels.get('cortisol', 0.20),
                    'oxytocin': self.levels.get('oxytocin', 0.40),
                    'endorphins': self.levels.get('endorphins', 0.35),
                    'melatonin': self.levels.get('gaba', 0.50),  # gaba → melatonin for compat
                    # New fields
                    'gaba': self.levels.get('gaba', 0.50),
                    'acetylcholine': self.levels.get('acetylcholine', 0.40),
                    'norepinephrine': self.levels.get('norepinephrine', 0.35),
                }
                
                # Update personality.hormone_baseline
                if 'personality' in brain_data:
                    brain_data['personality']['hormone_baseline'] = {
                        'adrenaline': self.baselines.get('norepinephrine', 0.35),
                        'serotonin': self.baselines.get('serotonin', 0.45),
                        'dopamine': self.baselines.get('dopamine', 0.50),
                        'cortisol': self.baselines.get('cortisol', 0.20),
                        'oxytocin': self.baselines.get('oxytocin', 0.40),
                        'endorphins': self.baselines.get('endorphins', 0.35),
                        'melatonin': self.baselines.get('gaba', 0.50),
                    }
                
                with open(self.brain_path, 'w') as f:
                    json.dump(brain_data, f, indent=2, default=str)
                    
        except Exception as e:
            logger.error(f"Failed to sync hormones to brain: {e}")
    
    # ========================================================================
    # CORE: Event Processing (Main Entry Point)
    # ========================================================================
    
    def process_event(self, event_type: str, details: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
        """
        Process an event and update all hormone levels.
        
        This is the MAIN entry point. Every significant event in the system
        should call this method.
        
        Args:
            event_type: One of EVENT_PROFILES keys, or 'custom'
            details: Optional dict with:
                - 'topic': topic string for RPE tracking
                - 'reward': actual reward value (0-1) for RPE
                - 'magnitude': scale factor for the event (default 1.0)
                - 'custom_impacts': dict of hormone→delta for custom events
        
        Returns:
            Dict of hormone deltas that were applied
        """
        details = details or {}
        now = time.time()
        magnitude = details.get('magnitude', 1.0)
        
        # 1. Get base hormone impacts for this event type
        base_impacts = EVENT_PROFILES.get(event_type, {})
        if not base_impacts and event_type != 'custom':
            logger.warning(f"Unknown event type: {event_type}, using custom impacts only")
        
        # Allow custom impacts to override/supplement
        custom_impacts = details.get('custom_impacts', {})
        
        # Merge: custom overrides base
        impacts = dict(base_impacts)
        impacts.update(custom_impacts)
        
        # 2. Apply Opponent Process (habituation)
        if event_type not in self.opponent_processes:
            self.opponent_processes[event_type] = OpponentProcess(
                a_magnitude=1.0,
                b_growth_rate=0.03,
                b_decay_rate=0.01,
            )
        
        op = self.opponent_processes[event_type]
        habituation_factor = op.expose(now)  # Returns 0.0-1.0, decreases with repetition
        
        # 3. Apply Schultz RPE for dopamine (if topic provided)
        rpe_delta = 0.0
        topic = details.get('topic', '')
        if topic and 'dopamine' in impacts:
            rpe_delta = self._compute_rpe(topic, details.get('reward', None), impacts.get('dopamine', 0))
            # RPE modulates the dopamine impact
            impacts['dopamine'] = rpe_delta
        
        # 4. Apply impacts with magnitude, habituation, and receptor sensitivity
        applied_deltas = {}
        for hormone, delta in impacts.items():
            if hormone not in self.levels:
                continue
            
            # Scale by magnitude and habituation
            # Habituation only reduces positive impacts (you don't habituate to pain as easily)
            if delta > 0:
                scaled_delta = delta * magnitude * habituation_factor
            else:
                scaled_delta = delta * magnitude * max(0.5, habituation_factor)  # Negative still reduced but less
            
            # Gate through receptor sensitivity (biological downregulation)
            # Desensitized receptors attenuate the impact; supersensitive amplify it
            receptor_gate = self.receptor_sensitivity.get(hormone, 1.0)
            scaled_delta *= receptor_gate
            
            # Diminishing returns near boundaries (receptor desensitization at extremes)
            # The closer to 0 or 1, the harder it is to push further
            old_level = self.levels[hormone]
            if scaled_delta > 0:
                # Pushing up: resistance increases as we approach 1.0
                headroom = max(0.0, 1.0 - old_level)
                damping = headroom ** 0.5  # sqrt gives soft resistance curve
                scaled_delta *= damping
            elif scaled_delta < 0:
                # Pushing down: resistance increases as we approach 0.0
                headroom = max(0.0, old_level)
                damping = headroom ** 0.5
                scaled_delta *= damping
            
            self.levels[hormone] = max(0.01, min(0.99, old_level + scaled_delta))
            applied_deltas[hormone] = self.levels[hormone] - old_level
        
        # 5. Apply cross-hormone interactions (Panksepp-inspired)
        #    Now gated through receptor sensitivity — desensitized receptors
        #    attenuate the cross-interaction effect (biological downregulation)
        self._apply_cross_interactions()
        
        # 6. Compute Panksepp circuit activations
        self._compute_circuit_activations()
        
        # 7. Track topic dopamine for priority adjustment
        if topic and 'dopamine' in applied_deltas:
            self._track_topic_dopamine(topic, applied_deltas['dopamine'])
        
        # 8. Update statistics
        self.stats['total_events_processed'] += 1
        self.stats['highest_dopamine'] = max(self.stats['highest_dopamine'], self.levels['dopamine'])
        self.stats['highest_cortisol'] = max(self.stats['highest_cortisol'], self.levels['cortisol'])
        self.stats['lowest_serotonin'] = min(self.stats['lowest_serotonin'], self.levels['serotonin'])
        
        # 9. Log event
        event_record = {
            'timestamp': now,
            'event_type': event_type,
            'topic': topic,
            'magnitude': magnitude,
            'habituation_factor': habituation_factor,
            'rpe_delta': rpe_delta,
            'applied_deltas': applied_deltas,
            'levels_after': dict(self.levels),
        }
        self.event_history.append(event_record)
        if len(self.event_history) > self.max_event_history:
            self.event_history = self.event_history[-self.max_event_history:]
        
        logger.info(f"🧪 Event '{event_type}': {self._format_deltas(applied_deltas)} | "
                     f"Habituation: {habituation_factor:.2f} | "
                     f"Levels: {self._format_levels()}")
        
        return applied_deltas
    
    # ========================================================================
    # Schultz RPE / TD-Learning (Dopamine)
    # ========================================================================
    
    def _compute_rpe(self, topic: str, actual_reward: Optional[float], base_dopamine_impact: float) -> float:
        """
        Compute Reward Prediction Error (Schultz, 1997).
        
        δ = r - V(s)
        
        Where:
            r = actual reward received
            V(s) = expected reward for this topic/context
        
        If δ > 0: Better than expected → dopamine spike (SEEKING activates)
        If δ ≈ 0: As expected → no change (learned prediction)
        If δ < 0: Worse than expected → dopamine dip (disappointment)
        
        V(s) is updated: V(s) ← V(s) + α * δ
        """
        # Derive domain from topic for generalization
        domain = self._get_topic_domain(topic)
        
        # Get expected reward (initialize at 0.5 = neutral expectation)
        V_s = self.expected_rewards.get(domain, 0.5)
        
        # If no explicit reward given, derive from the base dopamine impact
        if actual_reward is None:
            actual_reward = max(0.0, min(1.0, 0.5 + base_dopamine_impact))
        
        # Compute RPE
        rpe = actual_reward - V_s
        
        # Update expected reward (TD-learning)
        self.expected_rewards[domain] = V_s + self.reward_learning_rate * rpe
        self.expected_rewards[domain] = max(0.0, min(1.0, self.expected_rewards[domain]))
        
        # Scale RPE to dopamine delta
        # Positive RPE → bigger dopamine spike than base would give
        # Negative RPE → dopamine dip
        dopamine_delta = rpe * 0.5  # Scale factor
        
        logger.debug(f"📊 RPE for '{domain}': r={actual_reward:.2f}, V(s)={V_s:.2f}, "
                      f"δ={rpe:.3f}, DA_delta={dopamine_delta:.3f}")
        
        return dopamine_delta
    
    def _get_topic_domain(self, topic: str) -> str:
        """Extract a general domain from a topic string for RPE generalization."""
        topic_lower = topic.lower()
        
        domain_keywords = {
            'technology': ['ai', 'machine learning', 'neural', 'algorithm', 'computing', 'software', 'code', 'programming'],
            'science': ['physics', 'chemistry', 'biology', 'quantum', 'molecular', 'evolution', 'genetic'],
            'mathematics': ['math', 'calculus', 'theorem', 'proof', 'topology', 'algebra', 'geometry'],
            'philosophy': ['consciousness', 'ethics', 'philosophy', 'metaphysics', 'epistemology', 'existential'],
            'creative': ['art', 'music', 'creative', 'design', 'aesthetic', 'imagination', 'fiction'],
            'social': ['community', 'social', 'culture', 'society', 'communication', 'relationship'],
            'engineering': ['robotics', 'system', 'architecture', 'optimization', 'infrastructure', 'hardware'],
            'self_evolution': ['self', 'personality', 'identity', 'growth', 'evolve', 'adapt', 'learn'],
        }
        
        for domain, keywords in domain_keywords.items():
            if any(kw in topic_lower for kw in keywords):
                return domain
        
        return 'general'
    
    # ========================================================================
    # Homeostatic Decay
    # ========================================================================
    
    def decay_tick(self, dt: float = 1.0):
        """
        Apply homeostatic decay toward baseline (Homeostatic Control Theory)
        with biologically-inspired tonic firing and boundary repulsion.
        
        Three forces act on each hormone per tick:
        
        1. Exponential decay: dH/dt = -decay_rate * (H - baseline)
           Models enzymatic clearance (MAO, COMT, AChE, etc.)
           
        2. Tonic baseline secretion: Neurons fire at 1-10 Hz even at rest,
           providing constant NT release. When levels drop far below baseline,
           autoreceptor feedback INCREASES tonic firing rate, creating a
           stronger pull back. This prevents levels from sticking at 0.0.
           
        3. Overflow dampening (quadratic): When levels are far above baseline,
           reuptake transporters saturate, enzymatic pathways upregulate,
           and negative feedback loops activate (e.g., cortisol→HPA axis).
           This is QUADRATIC — the closer to ceiling, the harder the pushback.
           Prevents levels from sticking at 1.0.
        
        Should be called once per evolution cycle.
        Also updates receptor sensitivity (the biological anti-saturation mechanism).
        """
        for hormone in HORMONES:
            current = self.levels.get(hormone, 0.5)
            baseline = self.baselines.get(hormone, 0.5)
            rate = self.decay_rates.get(hormone, 0.05)
            
            # 1. Standard exponential decay toward baseline (enzymatic clearance)
            delta = -rate * (current - baseline) * dt
            
            # 2. Tonic baseline secretion (basal neural firing)
            # Biology: even without stimulation, neurons maintain a resting firing
            # rate. When NT is depleted (autoreceptor feedback), tonic rate increases.
            # This creates a FLOOR — NT can't stay at 0.0 in a living brain.
            distance_below = max(0.0, baseline - current)
            tonic_secretion = 0.025 * distance_below * dt
            
            # 3. Overflow dampening (reuptake overflow + enzymatic upregulation)
            # Biology: At high concentrations, DAT/SERT/NET reuptake transporters
            # saturate, but enzymatic degradation paths upregulate. HPA negative
            # feedback kicks in for cortisol. The net effect is a CEILING force
            # that grows quadratically — it's very hard to sustain NT at 1.0.
            distance_above = max(0.0, current - baseline)
            overflow_dampening = -0.02 * distance_above * distance_above * dt
            
            total_delta = delta + tonic_secretion + overflow_dampening
            
            # Soft floor at 0.01, soft ceiling at 0.99
            # Biology: true zero neurotransmitter is incompatible with life;
            # true saturation would cause receptor damage / excitotoxicity
            self.levels[hormone] = max(0.01, min(0.99, current + total_delta))
        
        # Update receptor sensitivity based on sustained deviation from baseline
        # This is the KEY biological mechanism that prevents saturation:
        # - Hormone stays high → receptors downregulate (desensitize)
        # - Hormone stays low  → receptors upregulate (supersensitize)
        # - Hormone at baseline → receptors normalize toward 1.0
        self._update_receptor_sensitivity(dt)
        
        # Decay opponent process B-strengths over time
        for op in self.opponent_processes.values():
            op.decay(dt)
        
        # Recompute Panksepp circuits from decayed hormone levels
        # (weighted by receptor sensitivity — desensitized circuits muted)
        self._compute_circuit_activations()
        
        self.stats['total_decay_ticks'] += 1
        
        logger.debug(f"🕐 Decay tick #{self.stats['total_decay_ticks']}: {self._format_levels()} | "
                     f"Receptor sensitivity: {self._format_receptor_sensitivity()}")
    
    def _update_receptor_sensitivity(self, dt: float = 1.0):
        """
        Model receptor down/upregulation based on sustained hormone levels.
        
        Biology:
        - Sustained HIGH levels → β-arrestin-mediated receptor internalization
          (downregulation). The neurotransmitter is present but its EFFECT weakens.
          Example: SSRI "poop out" — chronic serotonin flooding causes 5-HT1A
          autoreceptor desensitization over weeks.
          
        - Sustained LOW levels → receptor upregulation (denervation supersensitivity).
          Fewer molecules needed to trigger the same postsynaptic response.
          Example: Dopamine receptor supersensitivity after prolonged antipsychotic use.
          
        - At BASELINE → receptors drift back toward normal sensitivity (1.0).
        
        The adaptation is asymmetric: downregulation is faster than upregulation,
        matching the biological reality that it's easier to lose sensitivity than
        to regain it.
        
        NOTE: Rates tuned after production showed NOR=0.00 (89%), COR=0.00 (99%),
        GABA=1.00 (25%). The original 0.015/0.008 rates were too slow to prevent
        saturation in an AI system where "minutes" pass in seconds.
        """
        for hormone in HORMONES:
            current = self.levels.get(hormone, 0.5)
            baseline = self.baselines.get(hormone, 0.5)
            sensitivity = self.receptor_sensitivity.get(hormone, 1.0)
            
            deviation = current - baseline
            
            if deviation > 0.05:
                # Above baseline → downregulate (desensitize)
                # Stronger deviation = faster downregulation
                downreg_delta = -self.receptor_downreg_rate * deviation * dt
                sensitivity = max(0.2, sensitivity + downreg_delta)  # Floor at 0.2 (never fully dead)
                
            elif deviation < -0.05:
                # Below baseline → upregulate (supersensitize)
                # Can go above 1.0 — supersensitivity is real
                upreg_delta = -self.receptor_upreg_rate * deviation * dt  # deviation is negative, so this is positive
                sensitivity = min(1.6, sensitivity + upreg_delta)  # Cap at 1.6 (higher supersensitivity)
                
            else:
                # Near baseline → normalize toward 1.0 (faster receptor turnover)
                sensitivity += (1.0 - sensitivity) * 0.02 * dt  # 2x faster normalization
            
            self.receptor_sensitivity[hormone] = sensitivity
    
    def _format_receptor_sensitivity(self) -> str:
        """Format receptor sensitivity for logging."""
        parts = []
        for h in HORMONES:
            s = self.receptor_sensitivity.get(h, 1.0)
            if abs(s - 1.0) > 0.02:  # Only show non-normal receptors
                parts.append(f"{h[:3]}={s:.2f}")
        return '{' + ', '.join(parts) + '}' if parts else '{all normal}'
    
    # ========================================================================
    # Cross-Hormone Interactions (Panksepp-inspired)
    # ========================================================================
    
    def _apply_cross_interactions(self):
        """
        Apply cross-hormone interactions, gated by receptor sensitivity.
        
        Each hormone influences others based on its current deviation from baseline.
        The TARGET hormone's receptor sensitivity modulates how strongly it receives
        the interaction — this is the biological mechanism that prevents saturation.
        
        When serotonin has been at 0.95 for a while, its receptors downregulate to ~0.6,
        so any cross-interaction trying to boost serotonin further gets multiplied by 0.6
        instead of 1.0. Meanwhile cortisol at 0.05 for a while upregulates to ~1.3,
        making it MORE responsive to anything trying to raise it back up.
        """
        # Compute deltas first, apply all at once (prevents order-dependency)
        deltas = {h: 0.0 for h in HORMONES}
        
        for source_hormone, targets in self.cross_interactions.items():
            if source_hormone not in self.levels:
                continue
            
            # Influence strength scales with deviation from baseline
            source_level = self.levels[source_hormone]
            source_baseline = self.baselines.get(source_hormone, 0.5)
            deviation = source_level - source_baseline
            
            # Only exert influence if meaningfully deviated (dead zone)
            if abs(deviation) < 0.05:
                continue
            
            for target_hormone, interaction_strength in targets.items():
                if target_hormone not in self.levels:
                    continue
                
                # Delta = interaction_strength * deviation
                # Positive interaction + positive deviation → boost target
                # Positive interaction + negative deviation → suppress target
                delta = interaction_strength * deviation
                deltas[target_hormone] += delta
        
        # Apply deltas gated by receptor sensitivity AND boundary dampening
        for hormone, delta in deltas.items():
            if abs(delta) > 0.001:  # Skip trivial changes
                capped_delta = max(-0.05, min(0.05, delta))  # Original cap
                
                # Gate through receptor sensitivity:
                # - Desensitized receptors (< 1.0) attenuate the effect
                # - Supersensitive receptors (> 1.0) amplify it
                receptor_gate = self.receptor_sensitivity.get(hormone, 1.0)
                capped_delta *= receptor_gate
                
                # Boundary dampening: weaken cross-interactions pushing toward extremes
                # Biology: at very low NT, postsynaptic feedback inhibits further
                # suppression. At very high NT, autoreceptor-mediated negative
                # feedback limits further release from presynaptic terminals.
                target_level = self.levels.get(hormone, 0.5)
                if capped_delta > 0 and target_level > 0.75:
                    # Pushing UP when already high → dampen
                    dampen = 1.0 - ((target_level - 0.75) / 0.25) * 0.8  # At 1.0: 20% effect
                    capped_delta *= max(0.1, dampen)
                elif capped_delta < 0 and target_level < 0.25:
                    # Pushing DOWN when already low → dampen
                    dampen = 1.0 - ((0.25 - target_level) / 0.25) * 0.8  # At 0.0: 20% effect
                    capped_delta *= max(0.1, dampen)
                
                self.levels[hormone] = max(0.01, min(0.99, self.levels[hormone] + capped_delta))
    
    def _compute_circuit_activations(self):
        """
        Compute Panksepp's affective circuit activations from hormone levels.
        
        Each circuit = primary_hormone_level + Σ(modulator * level) + Σ(inhibitor * level)
        
        Effective hormone contribution is modulated by receptor sensitivity:
        a desensitized receptor means the hormone is present but less effective.
        """
        for circuit_name, config in PANKSEPP_CIRCUITS.items():
            primary = config['primary']
            # Effective level = raw level * receptor sensitivity
            primary_sensitivity = self.receptor_sensitivity.get(primary, 1.0)
            activation = self.levels.get(primary, 0.0) * primary_sensitivity
            
            # Add modulator contributions (gated by their receptor sensitivity)
            for modulator, weight in config.get('modulators', {}).items():
                mod_sensitivity = self.receptor_sensitivity.get(modulator, 1.0)
                activation += weight * self.levels.get(modulator, 0.0) * mod_sensitivity
            
            # Add inhibitor contributions (negative weights, also gated)
            for inhibitor, weight in config.get('inhibitors', {}).items():
                inh_sensitivity = self.receptor_sensitivity.get(inhibitor, 1.0)
                activation += weight * self.levels.get(inhibitor, 0.0) * inh_sensitivity
            
            self.circuit_activations[circuit_name] = max(0.0, min(1.0, activation))
    
    # ========================================================================
    # Cañamero's Deficit-Driven Motivation
    # ========================================================================
    
    def get_deficits(self) -> Dict[str, float]:
        """
        Compute Cañamero deficit levels.
        
        Deficit = max(0, baseline - current_level)
        
        Higher deficit = stronger drive to restore that hormone.
        This creates organic motivation patterns:
        - Low dopamine → drive to seek rewarding activities
        - Low serotonin → drive for comforting/familiar tasks
        - Low oxytocin → drive for social interaction
        - High cortisol → drive to resolve/escape stressor
        """
        deficits = {}
        for hormone in HORMONES:
            current = self.levels.get(hormone, 0.5)
            baseline = self.baselines.get(hormone, 0.5)
            
            if hormone == 'cortisol':
                # For cortisol, deficit is when it's ABOVE baseline (drive to reduce stress)
                deficits[hormone] = max(0.0, current - baseline)
            else:
                # For all others, deficit is when below baseline
                deficits[hormone] = max(0.0, baseline - current)
        
        return deficits
    
    def get_drive_priorities(self) -> List[Dict[str, Any]]:
        """
        Get prioritized list of drives based on Cañamero deficits.
        
        Returns drives sorted by urgency, each with:
        - drive_name: human-readable name
        - hormone: which hormone is deficient
        - deficit: how deficient (0-1)
        - recommended_action: what behavior would satisfy this drive
        - topic_boost_keywords: topics that would help satisfy this drive
        """
        deficits = self.get_deficits()
        
        drive_map = {
            'dopamine': {
                'drive_name': 'SEEKING',
                'recommended_action': 'Explore novel topics, attempt challenging problems',
                'topic_boost_keywords': ['novel', 'discover', 'create', 'invent', 'explore', 'build'],
                'panksepp_circuit': 'SEEKING',
            },
            'serotonin': {
                'drive_name': 'CONTENTMENT',
                'recommended_action': 'Review and consolidate known knowledge, reflect on achievements',
                'topic_boost_keywords': ['review', 'consolidate', 'reflect', 'understand', 'integrate'],
                'panksepp_circuit': 'CARE',
            },
            'norepinephrine': {
                'drive_name': 'AROUSAL',
                'recommended_action': 'Engage with stimulating or urgent tasks',
                'topic_boost_keywords': ['urgent', 'complex', 'challenge', 'debug', 'solve'],
                'panksepp_circuit': 'SEEKING',
            },
            'cortisol': {
                'drive_name': 'STRESS_RELIEF',
                'recommended_action': 'Resolve pending errors or fix known problems',
                'topic_boost_keywords': ['fix', 'resolve', 'repair', 'error', 'debug', 'problem'],
                'panksepp_circuit': 'FEAR',
            },
            'oxytocin': {
                'drive_name': 'SOCIAL_CONNECTION',
                'recommended_action': 'Prepare content for user interaction, be helpful',
                'topic_boost_keywords': ['help', 'teach', 'explain', 'communicate', 'share'],
                'panksepp_circuit': 'CARE',
            },
            'endorphins': {
                'drive_name': 'PLAY',
                'recommended_action': 'Experiment creatively, try unconventional approaches',
                'topic_boost_keywords': ['play', 'experiment', 'creative', 'fun', 'imagine', 'art'],
                'panksepp_circuit': 'PLAY',
            },
            'gaba': {
                'drive_name': 'CALM',
                'recommended_action': 'Slow down, process one thing at a time, avoid overload',
                'topic_boost_keywords': ['simple', 'organize', 'sort', 'tidy', 'maintain'],
                'panksepp_circuit': None,
            },
            'acetylcholine': {
                'drive_name': 'LEARNING',
                'recommended_action': 'Deep-dive into a single topic, focus attention',
                'topic_boost_keywords': ['study', 'learn', 'research', 'analyze', 'investigate', 'deep'],
                'panksepp_circuit': 'SEEKING',
            },
        }
        
        drives = []
        for hormone, deficit in deficits.items():
            if deficit < 0.03:  # Skip negligible deficits
                continue
            
            info = drive_map.get(hormone, {
                'drive_name': hormone.upper(),
                'recommended_action': 'Unknown',
                'topic_boost_keywords': [],
                'panksepp_circuit': None,
            })
            
            drives.append({
                'hormone': hormone,
                'deficit': deficit,
                **info,
            })
        
        # Sort by deficit (most urgent first)
        drives.sort(key=lambda d: d['deficit'], reverse=True)
        
        return drives
    
    # ========================================================================
    # Lövheim's Cube of Emotion
    # ========================================================================
    
    def get_emotional_state(self) -> Dict[str, float]:
        """
        Compute emotional state using Lövheim's Cube (2012).
        
        Maps (serotonin, dopamine, norepinephrine) to 8 basic emotions.
        Each emotion's activation is inversely proportional to its distance
        from the emotional point in the cube.
        
        Also adds SAIGE-specific composite emotions:
        - curiosity: interest + SEEKING circuit
        - frustration: anger + RAGE circuit
        - empathy: enjoyment + CARE circuit
        - alertness: surprise + FEAR circuit (mild)
        """
        s = self.levels.get('serotonin', 0.5)
        d = self.levels.get('dopamine', 0.5)
        n = self.levels.get('norepinephrine', 0.5)
        
        emotions = {}
        
        for emotion_name, corner in LOVHEIM_CUBE.items():
            # Distance from current state to this emotion's corner
            cs = corner['serotonin']
            cd = corner['dopamine']
            cn = corner['norepinephrine']
            
            # Euclidean distance in 3D cube
            dist = math.sqrt((s - cs)**2 + (d - cd)**2 + (n - cn)**2)
            
            # Max distance in unit cube = sqrt(3) ≈ 1.73
            max_dist = math.sqrt(3)
            
            # Activation is inversely proportional to distance
            # Using Gaussian-like falloff for smoother transitions
            activation = math.exp(-2.5 * dist**2)
            
            emotions[emotion_name] = activation
        
        # ---- SAIGE composite emotions (for backward compatibility) ----
        emotions['curiosity'] = (
            0.5 * emotions.get('interest', 0) +
            0.3 * self.circuit_activations.get('SEEKING', 0) +
            0.2 * self.levels.get('acetylcholine', 0.5)
        )
        
        emotions['frustration'] = (
            0.4 * emotions.get('anger', 0) +
            0.3 * self.circuit_activations.get('RAGE', 0) +
            0.3 * max(0, self.levels.get('cortisol', 0.2) - 0.3)
        )
        
        emotions['joy'] = (
            0.5 * emotions.get('enjoyment', 0) +
            0.3 * self.circuit_activations.get('PLAY', 0) +
            0.2 * self.levels.get('endorphins', 0.35)
        )
        
        emotions['empathy'] = (
            0.4 * self.circuit_activations.get('CARE', 0) +
            0.3 * self.levels.get('oxytocin', 0.4) +
            0.3 * emotions.get('enjoyment', 0)
        )
        
        emotions['alertness'] = (
            0.4 * emotions.get('surprise', 0) +
            0.3 * self.levels.get('norepinephrine', 0.35) +
            0.3 * self.levels.get('acetylcholine', 0.4)
        )
        
        # Clamp all values
        for k in emotions:
            emotions[k] = max(0.0, min(1.0, emotions[k]))
        
        return emotions
    
    # ========================================================================
    # Topic Priority Boosting (Behavior-Driving Interface)
    # ========================================================================
    
    def get_topic_priority_boost(self, topic: str, domain: str = '') -> float:
        """
        Calculate how much to boost a topic's priority based on hormonal state.
        
        This is the KEY behavior-driving method. It transforms hormone levels
        into concrete topic selection biases.
        
        Returns a score from -0.5 to +0.5 that should be ADDED to the
        base topic approval score.
        
        Factors:
        1. Dopamine affinity — topics previously associated with dopamine spikes
        2. Deficit-driven priority — topics matching current drive needs
        3. Cortisol urgency — stress pushes toward problem-fixing topics
        4. Novelty bonus — moderate dopamine + low GABA → novelty seeking
        5. RPE curiosity — topics with high prediction uncertainty
        """
        boost = 0.0
        topic_lower = topic.lower()
        domain = domain or self._get_topic_domain(topic)
        
        # --- 1. Dopamine Affinity (learned associations) ---
        topic_history = self.topic_dopamine_history.get(domain, [])
        if topic_history:
            avg_dopamine = sum(topic_history[-10:]) / len(topic_history[-10:])
            # Positive history → boost, negative history → suppress
            boost += avg_dopamine * 0.3  # Scale to max ±0.15
        
        # --- 2. Deficit-Driven Priority ---
        drives = self.get_drive_priorities()
        for drive in drives[:3]:  # Top 3 drives
            keywords = drive.get('topic_boost_keywords', [])
            if any(kw in topic_lower for kw in keywords):
                # Boost proportional to deficit strength
                boost += drive['deficit'] * 0.25  # Max ~0.25 per drive match
                break  # Only apply strongest matching drive
        
        # --- 3. Cortisol Urgency ---
        cortisol = self.levels.get('cortisol', 0.2)
        cortisol_baseline = self.baselines.get('cortisol', 0.2)
        if cortisol > cortisol_baseline + 0.1:
            # Under stress → boost problem-fixing topics
            stress_keywords = ['fix', 'error', 'debug', 'problem', 'issue', 'resolve', 'repair', 'bug']
            if any(kw in topic_lower for kw in stress_keywords):
                boost += (cortisol - cortisol_baseline) * 0.3
            else:
                # Penalize non-urgent exploration when stressed
                boost -= (cortisol - cortisol_baseline) * 0.1
        
        # --- 4. Novelty Seeking ---
        dopamine = self.levels.get('dopamine', 0.5)
        dopamine_baseline = self.baselines.get('dopamine', 0.5)
        gaba = self.levels.get('gaba', 0.5)
        
        if dopamine < dopamine_baseline - 0.05 and gaba < 0.5:
            # Low dopamine + not calm → seeking novelty
            if domain not in self.expected_rewards or len(topic_history) < 3:
                boost += 0.15  # Novelty bonus for unexplored domains
        
        # --- 5. RPE Curiosity (high prediction uncertainty) ---
        expected = self.expected_rewards.get(domain, 0.5)
        # Domains close to 0.5 (uncertain) are more interesting than confident ones
        uncertainty = 1.0 - abs(expected - 0.5) * 2  # Peaks at expected=0.5
        if uncertainty > 0.7:
            boost += 0.05  # Small curiosity bonus for uncertain domains
        
        # --- 6. SEEKING circuit activation ---
        seeking = self.circuit_activations.get('SEEKING', 0)
        if seeking > 0.5:
            # High SEEKING → boost all exploration
            boost += (seeking - 0.5) * 0.1
        
        # Clamp total boost
        return max(-0.5, min(0.5, boost))
    
    def _track_topic_dopamine(self, topic: str, dopamine_delta: float):
        """Track dopamine response to topics for learned associations."""
        domain = self._get_topic_domain(topic)
        
        self.topic_dopamine_history[domain].append(dopamine_delta)
        
        # Keep history bounded
        if len(self.topic_dopamine_history[domain]) > self.max_topic_history:
            self.topic_dopamine_history[domain] = self.topic_dopamine_history[domain][-self.max_topic_history:]
    
    # ========================================================================
    # Behavioral Outputs (What the rest of the system reads)
    # ========================================================================
    
    def get_behavior_modifiers(self) -> Dict[str, float]:
        """
        Get high-level behavioral modifiers derived from hormone state.
        
        Uses EFFECTIVE levels (raw × receptor_sensitivity) to model the fact that
        desensitized receptors reduce the functional impact of a neurotransmitter
        even if raw levels are high. This is how SSRIs "poop out" — serotonin is
        elevated but 5-HT receptors have downregulated.
        
        These modify SAIGE's overall behavior patterns:
        - exploration_drive: How much to seek new topics vs. consolidate
        - risk_tolerance: Willingness to try unconventional approaches
        - focus_depth: How deep to go into a single topic vs. breadth
        - social_drive: How much to prioritize human interaction
        - creative_drive: Tendency toward creative/artistic outputs
        - urgency: How much to prioritize urgent/fixing tasks
        - patience: How long to persist on a difficult chain
        """
        modifiers = {}
        
        # Use effective levels: raw level × receptor sensitivity
        # This is the TRUE functional neurotransmitter signal
        def eff(hormone: str, default: float = 0.5) -> float:
            raw = self.levels.get(hormone, default)
            sens = self.receptor_sensitivity.get(hormone, 1.0)
            return max(0.0, min(1.0, raw * sens))
        
        dopamine = eff('dopamine', 0.5)
        serotonin = eff('serotonin', 0.45)
        norepinephrine = eff('norepinephrine', 0.35)
        cortisol = eff('cortisol', 0.2)
        oxytocin = eff('oxytocin', 0.4)
        endorphins = eff('endorphins', 0.35)
        gaba = eff('gaba', 0.5)
        ach = eff('acetylcholine', 0.4)
        
        # Exploration = high dopamine + low serotonin (not content) + low GABA (not too calm)
        modifiers['exploration_drive'] = (
            0.4 * dopamine + 0.3 * (1 - serotonin) + 0.2 * (1 - gaba) + 0.1 * norepinephrine
        )
        
        # Risk tolerance = high dopamine + high endorphins + low cortisol
        modifiers['risk_tolerance'] = (
            0.4 * dopamine + 0.3 * endorphins + 0.3 * (1 - cortisol)
        )
        
        # Focus depth = high acetylcholine + high GABA (calm focus) + low norepinephrine (not too aroused)
        modifiers['focus_depth'] = (
            0.4 * ach + 0.3 * gaba + 0.3 * (1 - norepinephrine * 0.5)
        )
        
        # Social drive = high oxytocin + moderate serotonin + low cortisol
        modifiers['social_drive'] = (
            0.5 * oxytocin + 0.3 * serotonin + 0.2 * (1 - cortisol)
        )
        
        # Creative drive = high dopamine + high endorphins + moderate norepinephrine
        modifiers['creative_drive'] = (
            0.4 * dopamine + 0.3 * endorphins + 0.2 * norepinephrine + 0.1 * (1 - cortisol)
        )
        
        # Urgency = high cortisol + high norepinephrine + low GABA
        modifiers['urgency'] = (
            0.4 * cortisol + 0.3 * norepinephrine + 0.3 * (1 - gaba)
        )
        
        # Patience = high serotonin + high GABA + low cortisol + low norepinephrine
        modifiers['patience'] = (
            0.3 * serotonin + 0.3 * gaba + 0.2 * (1 - cortisol) + 0.2 * (1 - norepinephrine)
        )
        
        # Clamp all
        for k in modifiers:
            modifiers[k] = max(0.0, min(1.0, modifiers[k]))
        
        return modifiers
    
    def get_dominant_circuit(self) -> Tuple[str, float]:
        """Get the currently dominant Panksepp circuit and its activation."""
        if not self.circuit_activations:
            return ('SEEKING', 0.5)
        
        dominant = max(self.circuit_activations.items(), key=lambda x: x[1])
        return dominant
    
    def get_hormone_summary(self) -> str:
        """Get a human-readable summary of current hormone state for logging."""
        lines = ["═══ HORMONE STATE ═══"]
        
        for h in HORMONES:
            level = self.levels.get(h, 0.5)
            baseline = self.baselines.get(h, 0.5)
            deficit = max(0, baseline - level) if h != 'cortisol' else max(0, level - baseline)
            bar_len = int(level * 20)
            bar = '█' * bar_len + '░' * (20 - bar_len)
            baseline_pos = int(baseline * 20)
            
            deficit_str = f" DEFICIT:{deficit:.2f}" if deficit > 0.05 else ""
            lines.append(f"  {h:16s} [{bar}] {level:.3f} (base: {baseline:.2f}){deficit_str}")
        
        # Circuit activations
        lines.append("  ─── Panksepp Circuits ───")
        dominant, dom_level = self.get_dominant_circuit()
        for circuit, activation in sorted(self.circuit_activations.items(), key=lambda x: -x[1]):
            marker = " ◄ DOMINANT" if circuit == dominant else ""
            lines.append(f"  {circuit:14s}: {activation:.3f}{marker}")
        
        # Top drives
        drives = self.get_drive_priorities()
        if drives:
            lines.append("  ─── Active Drives ───")
            for drive in drives[:3]:
                lines.append(f"  {drive['drive_name']:14s}: deficit={drive['deficit']:.3f} → {drive['recommended_action'][:50]}")
        
        return "\n".join(lines)
    
    # ========================================================================
    # LLM Sampling Parameter Modulation
    # ========================================================================
    
    def get_sampling_parameters(self) -> Dict[str, Any]:
        """
        Map hormone state directly to llama.cpp sampling parameters.
        
        This is the bridge between neurochemistry and neural network inference.
        Each mapping is grounded in real neuroscience:
        
        DOPAMINE → exploration, novelty, reward-seeking
          - temperature: High DA = broader sampling (explore), low DA = conservative
          - presence_penalty: High DA = avoid re-treading, seek novelty
          - frequency_penalty: High DA = penalize repetition harder
        
        SEROTONIN → mood stability, contentment, impulse control
          - min_p: High 5-HT = higher quality floor (contentment → only good outputs)
          - typical_p: High 5-HT = more typical/normal outputs
        
        NOREPINEPHRINE → arousal, attention, fight-or-flight
          - top_k: High NE = narrower focus (tunnel vision under arousal)
          - dynatemp_range: High NE = more volatile temperature swings
        
        CORTISOL → stress, threat detection, urgency
          - max_tokens: High cortisol = shorter responses (urgency)
          - repeat_penalty: High cortisol = rigid, repetitive thinking
          - top_k: High cortisol tightens focus further
        
        ACETYLCHOLINE → focused attention, memory, learning
          - top_k: High ACh = precise token selection (crystallized attention)
          - repeat_last_n: High ACh = longer memory window for repeats
        
        GABA → inhibition, calm, prevents overstimulation
          - temperature: High GABA dampens temperature (calm → conservative)
          - dynatemp_range: High GABA = stable temperature (no swings)
        
        OXYTOCIN → social bonding, empathy, trust
          - presence_penalty: Low OT = less penalty (staying on topic = social focus)
        
        ENDORPHINS → pleasure, play, creativity
          - typical_p: High endorphins = allow atypical outputs (playfulness)
          - temperature: Mild upward push (euphoria loosens constraints)
        
        Returns dict ready to merge into llama.cpp /completion or /v1/chat/completions
        """
        # Get effective levels (raw × receptor sensitivity)
        def eff(hormone: str, default: float = 0.5) -> float:
            raw = self.levels.get(hormone, default)
            sens = self.receptor_sensitivity.get(hormone, 1.0)
            return max(0.0, min(1.0, raw * sens))
        
        da = eff('dopamine', 0.5)          # Dopamine
        ser = eff('serotonin', 0.45)       # Serotonin
        ne = eff('norepinephrine', 0.35)   # Norepinephrine
        cort = eff('cortisol', 0.2)        # Cortisol
        oxy = eff('oxytocin', 0.4)         # Oxytocin
        endo = eff('endorphins', 0.35)     # Endorphins
        gaba_val = eff('gaba', 0.5)        # GABA
        ach = eff('acetylcholine', 0.4)    # Acetylcholine
        
        # ---- TEMPERATURE ----
        # Base 0.7. Dopamine pushes up (exploration), GABA pushes down (calm/conservative),
        # Endorphins push up mildly (playful looseness), Cortisol pushes down (rigid under stress)
        temperature = 0.7 + 0.35 * (da - 0.5) - 0.25 * (gaba_val - 0.5) + 0.15 * (endo - 0.35) - 0.2 * (cort - 0.2)
        temperature = max(0.3, min(1.3, temperature))
        
        # ---- TOP_K ----
        # Base 40. Norepinephrine narrows focus (tunnel vision), Acetylcholine narrows (precision),
        # Cortisol narrows (threat narrowing), Dopamine widens (curiosity about many options)
        top_k_raw = 40 + 30 * (da - 0.5) - 25 * (ne - 0.35) - 20 * (ach - 0.4) - 15 * (cort - 0.2)
        top_k = max(10, min(80, int(top_k_raw)))
        
        # ---- TOP_P ----
        # Base 0.9. Dopamine widens, Serotonin narrows (content with fewer options)
        top_p = 0.9 + 0.08 * (da - 0.5) - 0.1 * (ser - 0.45)
        top_p = max(0.7, min(0.98, top_p))
        
        # ---- MIN_P ----
        # Base 0.05. Serotonin raises floor (quality control), Cortisol raises (conservative under stress)
        min_p = 0.05 + 0.08 * (ser - 0.45) + 0.05 * (cort - 0.2)
        min_p = max(0.01, min(0.2, min_p))
        
        # ---- TYPICAL_P ----
        # Base 1.0 (disabled). Serotonin enables it (preferring typical/normal outputs),
        # Endorphins disable it (playfulness allows atypical)
        typical_p = 1.0 - 0.15 * (ser - 0.45) + 0.1 * (endo - 0.35)
        typical_p = max(0.8, min(1.0, typical_p))
        
        # ---- FREQUENCY_PENALTY ----
        # Base 0.3. Dopamine increases (novelty-seeking avoids repetition)
        frequency_penalty = 0.3 + 0.3 * (da - 0.5)
        frequency_penalty = max(0.0, min(0.8, frequency_penalty))
        
        # ---- PRESENCE_PENALTY ----
        # Base 0.0. Dopamine increases (explore new tokens), Oxytocin decreases (staying on-topic = bonding)
        presence_penalty = 0.0 + 0.3 * (da - 0.5) - 0.15 * (oxy - 0.4)
        presence_penalty = max(0.0, min(0.6, presence_penalty))
        
        # ---- REPEAT_PENALTY ----
        # Base 1.1. Cortisol increases (stress → rigid perseveration, but we penalize it to break the loop)
        # Dopamine increases (exploration seeks novelty)
        repeat_penalty = 1.1 + 0.2 * (da - 0.5) + 0.15 * (cort - 0.2)
        repeat_penalty = max(1.0, min(1.5, repeat_penalty))
        
        # ---- REPEAT_LAST_N ----
        # Base 64. Acetylcholine increases (longer attention/memory span for repeat detection)
        repeat_last_n = int(64 + 40 * (ach - 0.4))
        repeat_last_n = max(32, min(128, repeat_last_n))
        
        # ---- DYNATEMP_RANGE ----
        # Base 0.0 (disabled). Norepinephrine enables it (arousal creates temperature volatility),
        # GABA suppresses it (calm = stable temperature)
        dynatemp_range = 0.0 + 0.3 * (ne - 0.35) - 0.2 * (gaba_val - 0.5)
        dynatemp_range = max(0.0, min(0.5, dynatemp_range))
        
        # ---- MAX_TOKENS ----
        # Base 800. Cortisol reduces (urgency → terse), GABA increases (calm → thorough),
        # Acetylcholine increases (attention sustains longer outputs)
        max_tokens = int(800 - 300 * max(0, cort - 0.3) + 200 * (gaba_val - 0.5) + 100 * (ach - 0.4))
        max_tokens = max(300, min(1200, max_tokens))
        
        params = {
            'temperature': round(temperature, 3),
            'top_k': top_k,
            'top_p': round(top_p, 3),
            'min_p': round(min_p, 3),
            'typical_p': round(typical_p, 3),
            'frequency_penalty': round(frequency_penalty, 3),
            'presence_penalty': round(presence_penalty, 3),
            'repeat_penalty': round(repeat_penalty, 3),
            'repeat_last_n': repeat_last_n,
            'dynatemp_range': round(dynatemp_range, 3),
            'max_tokens': max_tokens,
        }
        
        logger.debug(f"🧬 Hormone→Sampling: temp={temperature:.2f} top_k={top_k} top_p={top_p:.2f} "
                     f"min_p={min_p:.3f} freq_pen={frequency_penalty:.2f} "
                     f"dynatemp={dynatemp_range:.2f} max_tok={max_tokens}")
        
        return params
    # Training Data Integration (for LoRA)
    # ========================================================================
    
    def get_hormone_context_for_training(self) -> Dict[str, Any]:
        """
        Get hormone context to embed in LoRA training data.
        
        This is how hormones influence the adapter weights:
        Training examples generated during high-dopamine states carry
        "reward" signal that reinforces similar behavior patterns.
        """
        return {
            'hormone_levels': dict(self.levels),
            'dominant_circuit': self.get_dominant_circuit()[0],
            'top_drives': [d['drive_name'] for d in self.get_drive_priorities()[:3]],
            'behavior_modifiers': self.get_behavior_modifiers(),
            'emotional_state': {k: round(v, 3) for k, v in self.get_emotional_state().items() 
                                if v > 0.1},  # Only significant emotions
        }
    
    # ========================================================================
    # Formatting Helpers
    # ========================================================================
    
    def _format_levels(self) -> str:
        """Format hormone levels as compact string."""
        parts = []
        for h in HORMONES:
            v = self.levels.get(h, 0)
            short_name = h[:3].upper()
            parts.append(f"{short_name}:{v:.2f}")
        return " ".join(parts)
    
    def _format_deltas(self, deltas: Dict[str, float]) -> str:
        """Format applied deltas as compact string."""
        parts = []
        for h, d in sorted(deltas.items(), key=lambda x: -abs(x[1])):
            if abs(d) < 0.001:
                continue
            sign = "+" if d > 0 else ""
            short_name = h[:3].upper()
            parts.append(f"{short_name}{sign}{d:.3f}")
        return " ".join(parts) if parts else "no change"
