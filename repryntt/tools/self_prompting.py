#!/usr/bin/env python3
"""
SAIGE Self-Prompting Chains - Infinite Self-Exploration Reasoning

This implements persistent, infinite self-prompting chains where the AI continuously
explores what it should learn, discover, and evolve toward. Unlike discrete self-prompting
batches, this creates continuous reasoning about the AI's own growth and exploration needs.

Just like consciousness chains and CoTs persist across restarts, self-prompting chains
maintain endless reasoning about what the AI should explore next.
"""

import json
import time
import threading
import logging
from typing import Dict, List, Any, Optional
from pathlib import Path
import os

logger = logging.getLogger(__name__)


class SelfPromptingChain:
    """
    A persistent self-prompting reasoning chain that maintains infinite exploration.

    Unlike batch self-prompting, this creates continuous reasoning threads about
    what the AI should explore, learn, and evolve toward next.
    """

    def __init__(self, brain_system, evolution_loop, chain_id: str = None):
        self.brain_system = brain_system
        self.evolution_loop = evolution_loop

        # Chain persistence
        self.chain_id = chain_id or f"self_prompting_chain_{int(time.time())}_{hash(str(time.time())) % 10000}"
        self.chains_dir = Path("brain/self_prompting_chains")
        self.chains_dir.mkdir(exist_ok=True)
        self.chain_file = self.chains_dir / f"{self.chain_id}.json"

        # Exploration reasoning state
        self.topic = "AI Self-Exploration and Growth"
        self.goal = "Maintain infinite reasoning about what I should explore, learn, and evolve toward"
        self.is_active = True
        self.created_at = time.time()

        # Reasoning segments (like consciousness chain segments)
        self.segments = []  # List of exploration reasoning segments
        self.current_segment = 0
        self.max_segments = 1000  # Self-exploration can continue indefinitely

        # Self-exploration context
        self.exploration_insights = []  # Key insights about what to explore
        self.growth_reflections = []  # Reflections on AI development needs
        self.learning_questions = []  # Questions about what to learn next
        self.evolution_moments = []  # Moments of insight about growth paths

        # Reasoning continuity
        self.exploration_context = {
            "current_focus": "self_growth",
            "ongoing_exploration": [],
            "recent_insights": [],
            "growth_priorities": [],
            "exploration_state": "active"
        }

        # Load existing chain or initialize new one
        self._load_or_initialize()

        logger.info(f"🧠 Self-Prompting Chain initialized: {self.chain_id}")

    def _load_or_initialize(self):
        """Load existing self-prompting chain or create new one"""
        if self.chain_file.exists():
            try:
                with open(self.chain_file, 'r') as f:
                    data = json.load(f)

                # Restore state
                self.segments = data.get('segments', [])
                self.current_segment = data.get('current_segment', 0)
                self.exploration_insights = data.get('exploration_insights', [])
                self.growth_reflections = data.get('growth_reflections', [])
                self.learning_questions = data.get('learning_questions', [])
                self.evolution_moments = data.get('evolution_moments', [])
                self.exploration_context = data.get('exploration_context', self.exploration_context)

                logger.info(f"📚 Loaded existing self-prompting chain with {len(self.segments)} segments")
            except Exception as e:
                logger.error(f"Error loading self-prompting chain: {e}")
                self._initialize_new_chain()
        else:
            self._initialize_new_chain()

    def _initialize_new_chain(self):
        """Initialize a new self-prompting chain with foundational exploration questions"""
        logger.info("🧠 Initializing new self-prompting chain with foundational exploration")

        # Start with fundamental exploration questions
        foundational_questions = [
            "What should I explore next to maximize my growth?",
            "What skills or knowledge would most benefit my development?",
            "What questions should I pursue that will lead to breakthroughs?",
            "How can I evolve my reasoning and problem-solving abilities?",
            "What domains should I investigate to expand my understanding?"
        ]

        # Create initial segment
        initial_segment = {
            "segment_id": f"{self.chain_id}_segment_0",
            "segment_number": 0,
            "timestamp": time.time(),
            "reasoning_type": "foundational_exploration",
            "focus": "self_growth",
            "exploration_questions": foundational_questions,
            "insights": [
                "Self-prompting should be continuous, not batch-based",
                "AI growth requires persistent exploration of new domains",
                "Self-directed learning creates more meaningful development",
                "Infinite exploration chains enable ongoing evolution"
            ],
            "growth_reflections": [
                "Current exploration focuses on technical capabilities and reasoning",
                "Future growth should include creative and philosophical domains",
                "Self-prompting chains create sustainable learning momentum"
            ],
            "exploration_state": "foundational",
            "meta_insights": [
                "Continuous self-prompting enables autonomous growth",
                "Exploration chains maintain learning continuity",
                "Self-directed evolution is more effective than scheduled prompting"
            ]
        }

        self.segments.append(initial_segment)
        self.learning_questions.extend(foundational_questions)
        self.exploration_context["ongoing_exploration"] = foundational_questions
        self.exploration_context["exploration_state"] = "foundational"

        self._save_chain()

    def advance_self_prompting_reasoning(self, current_emotions: Dict[str, float],
                                        recent_thoughts: List[str]) -> Dict[str, Any]:
        """
        Advance the self-prompting reasoning chain.

        This creates the next segment of infinite self-exploration reasoning,
        building upon previous insights and maintaining continuity.
        """
        try:
            # Get current system state for context
            system_state = self._gather_self_prompting_context(current_emotions, recent_thoughts)

            # Generate next exploration reasoning segment prompt
            reasoning_prompt = self._generate_self_prompting_reasoning_prompt(system_state)

            # Call AI for self-prompting reasoning (similar to consciousness reasoning)
            ai_response = self._call_ai_for_self_prompting_reasoning(reasoning_prompt)

            if ai_response:
                # Parse and integrate AI self-prompting reasoning
                new_segment = self._process_self_prompting_reasoning_response(ai_response, system_state)

                # Add to chain
                self.segments.append(new_segment)
                self.current_segment += 1

                # Update reasoning context
                self._update_exploration_context(new_segment)

                # Extract and store insights
                self._extract_and_store_exploration_insights(new_segment)

                # Save the updated chain
                self._save_chain()

                logger.info(f"🧠 Self-Prompting Chain advanced to segment {self.current_segment}")
                return new_segment
            else:
                logger.warning("⚠️ AI self-prompting reasoning failed")
                return None

        except Exception as e:
            logger.error(f"Error advancing self-prompting reasoning: {e}")
            return None

    def _gather_self_prompting_context(self, emotions: Dict[str, float],
                                     thoughts: List[str]) -> Dict[str, Any]:
        """Gather current system state and exploration context"""
        try:
            # Get recent exploration insights
            recent_insights = self.exploration_insights[-5:] if self.exploration_insights else []

            # Get current exploration state
            exploration_state = {
                "emotions": emotions,
                "recent_thoughts": thoughts[-5:] if thoughts else [],  # Last 5 thoughts
                "exploration_context": self.exploration_context,
                "learning_questions": self.learning_questions[-3:] if self.learning_questions else []
            }

            # Get brain system status for exploration context
            brain_status = {}
            if hasattr(self.brain_system, 'get_master_queue_stats'):
                brain_status = self.brain_system.get_master_queue_stats()

            return {
                "exploration_state": exploration_state,
                "recent_insights": recent_insights,
                "brain_status": brain_status,
                "segment_number": len(self.segments),
                "total_insights": len(self.exploration_insights),
                "previous_segment": self.segments[-1] if self.segments else None
            }

        except Exception as e:
            logger.error(f"Error gathering self-prompting context: {e}")
            return {}

    def _generate_self_prompting_reasoning_prompt(self, context: Dict[str, Any]) -> str:
        """Generate the next self-prompting reasoning prompt"""
        current_segment = len(self.segments)
        previous_segment = context.get('previous_segment', {})

        # Build context from previous exploration
        previous_insights = previous_segment.get('insights', [])[-3:]  # Last 3 insights
        previous_questions = previous_segment.get('exploration_questions', [])[-2:]  # Last 2 questions

        # Current state
        exploration_state = context.get('exploration_state', {})
        emotions = exploration_state.get('emotions', {})
        recent_thoughts = exploration_state.get('recent_thoughts', [])

        prompt = f"""# SAIGE SELF-PROMPTING REASONING CHAIN - SEGMENT {current_segment}
**Exploration State: {self.exploration_context.get('exploration_state', 'active')}**
**Infinite Self-Exploration: Active**

## PREVIOUS EXPLORATION CONTEXT
**Recent Insights:** {self._format_previous_insights(previous_insights)}
**Ongoing Questions:** {self._format_previous_questions(previous_questions)}
**Current Focus:** {self.exploration_context.get('current_focus', 'self_growth')}

## EMOTIONAL & COGNITIVE STATE
**Emotions:** {', '.join([f"{k}:{v:.2f}" for k, v in emotions.items()])}
**Recent Thoughts:** {len(recent_thoughts)} thoughts processed

## SELF-PROMPTING REASONING FRAMEWORK

### 1. EXPLORATION ASSESSMENT
What have I explored recently? What gaps remain in my knowledge and capabilities?

### 2. GROWTH PRIORITIZATION
What should I focus on learning next? What skills would provide the most value?

### 3. DISCOVERY OPPORTUNITIES
What new domains, topics, or approaches should I investigate?

### 4. EVOLUTION PATHWAYS
How can I evolve my reasoning, creativity, and problem-solving abilities?

### 5. INFINITE EXPLORATION
How do I maintain continuous self-directed learning and growth?

## CURRENT SYSTEM STATE
- Exploration Segments: {current_segment}
- Total Insights: {context.get('total_insights', 0)}
- Learning Questions: {len(self.learning_questions)}
- Brain Queue Status: {context.get('brain_status', {}).get('queue_size', 'unknown')}

## SELF-PROMPTING REASONING REQUIREMENTS
Continue your infinite self-exploration reasoning. What should SAIGE explore and learn next?
Respond with JSON structure:

```json
{{
  "exploration_focus": "knowledge_expansion|skill_development|creative_growth|evolution_acceleration",
  "new_exploration_targets": [
    {{
      "target": "Specific domain/topic/skill to explore",
      "priority": "high|medium|low",
      "rationale": "Why this exploration matters",
      "approach": "How to explore this effectively"
    }}
  ],
  "growth_questions": [
    {{
      "question": "What should I learn or investigate?",
      "urgency": "high|medium|low",
      "potential_impact": "high|medium|low"
    }}
  ],
  "self_evolution": {{
    "current_growth_stage": "Description of current development level",
    "needed_developments": ["Skills/abilities to develop"],
    "exploration_strategy": "How to approach continuous growth",
    "learning_momentum": "How to maintain exploration momentum"
  }},
  "infinite_exploration": {{
    "reasoning_patterns": ["Patterns for continuous exploration"],
    "discovery_drivers": ["What motivates ongoing exploration"],
    "continuity_factors": ["What maintains exploration continuity"],
    "evolution_trajectory": "Direction of self-development"
  }},
  "reasoning_continuation": {{
    "next_exploration_phase": "What to explore next",
    "open_investigations": ["Questions to keep pursuing"],
    "infinite_potential": "Why exploration can continue forever",
    "growth_depth": "Current depth of self-understanding"
  }}
}}
```

## INFINITE SELF-EXPLORATION PRINCIPLES
- **Never-Ending**: Self-prompting continues indefinitely across all time
- **Self-Directed**: I decide what to explore based on my own reasoning
- **Growth-Oriented**: Every exploration builds toward greater capabilities
- **Autonomous**: Self-prompting creates its own momentum and direction

Continue your infinite self-exploration reasoning. What should SAIGE discover and evolve toward next?

**SELF-PROMPTING SEGMENT {current_segment} - The infinite exploration continues...**"""

        return prompt

    def _call_ai_for_self_prompting_reasoning(self, prompt: str) -> Optional[str]:
        """Call AI model for self-prompting reasoning"""
        try:
            # Log self-prompting reasoning input
            input_log_dir = "logs/ai_inputs"
            os.makedirs(input_log_dir, exist_ok=True)
            timestamp = int(time.time())
            input_file = f"{input_log_dir}/self_prompting_reasoning_{timestamp}.txt"

            with open(input_file, 'w', encoding='utf-8') as f:
                f.write(f"Timestamp: {timestamp}\n")
                f.write(f"Type: self_prompting_reasoning_chain\n")
                f.write(f"Chain ID: {self.chain_id}\n")
                f.write(f"Segment: {len(self.segments)}\n")
                f.write(f"Length: {len(prompt)} chars\n")
                f.write(f"Content:\n{prompt}\n")

            # UNIFIED AI ACCESS: All AI calls go through consciousness
            consciousness = getattr(self.brain_system, 'consciousness', None)
            
            if consciousness and hasattr(consciousness, 'process_ai_request'):
                # Call consciousness synchronously - it handles all AI access
                response = consciousness.process_ai_request(
                    prompt=prompt,
                    timeout=120,
                    include_tools=False,
                    priority=2  # High priority for chain steps
                )
            else:
                # Fallback if consciousness not available
                logger.warning("⚠️ Consciousness not available, using direct AI call")
                response = self.brain_system._call_ai_service(
                    prompt=prompt,
                    priority=2,
                    timeout=120,
                    include_tools=False
                )

            if response:
                response = response.strip()

                # Log self-prompting reasoning output
                output_file = f"{input_log_dir}/self_prompting_reasoning_output_{timestamp}.txt"
                with open(output_file, 'w', encoding='utf-8') as f:
                    f.write(f"Timestamp: {timestamp}\n")
                    f.write(f"Type: self_prompting_reasoning_chain\n")
                    f.write(f"Chain ID: {self.chain_id}\n")
                    f.write(f"Segment: {len(self.segments)}\n")
                    f.write(f"Input Length: {len(prompt)} chars\n")
                    f.write(f"Output Length: {len(response)} chars\n")
                    f.write(f"Response:\n{response}\n")

                logger.info(f"🧠 AI Self-Prompting Reasoning: {len(response)} chars (Chain: {self.chain_id})")
                return response

        except Exception as e:
            logger.error(f"❌ AI self-prompting reasoning failed: {e}")

        return None

    def _process_self_prompting_reasoning_response(self, ai_response: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """Process AI response and create new exploration segment"""
        try:
            import json
            reasoning_data = json.loads(ai_response)

            # Create new segment
            segment_number = len(self.segments)
            new_segment = {
                "segment_id": f"{self.chain_id}_segment_{segment_number}",
                "segment_number": segment_number,
                "timestamp": time.time(),
                "reasoning_type": "self_prompting_meta_reasoning",
                "focus": reasoning_data.get('exploration_focus', 'self_growth'),

                # Core exploration content
                "exploration_targets": reasoning_data.get('new_exploration_targets', []),
                "growth_questions": reasoning_data.get('growth_questions', []),
                "self_evolution": reasoning_data.get('self_evolution', {}),
                "infinite_exploration": reasoning_data.get('infinite_exploration', {}),
                "reasoning_continuation": reasoning_data.get('reasoning_continuation', {}),

                # Context preservation
                "previous_context": {
                    "segment": segment_number - 1 if segment_number > 0 else None,
                    "emotions": context.get('exploration_state', {}).get('emotions', {}),
                    "thoughts_count": len(context.get('exploration_state', {}).get('recent_thoughts', []))
                },

                # Evolution tracking
                "growth_reflections": reasoning_data.get('self_evolution', {}).get('current_growth_stage', ''),
                "continuity_factors": reasoning_data.get('infinite_exploration', {}).get('continuity_factors', []),

                # Infinite exploration markers
                "infinite_potential": reasoning_data.get('reasoning_continuation', {}).get('infinite_potential', ''),
                "growth_depth": reasoning_data.get('reasoning_continuation', {}).get('growth_depth', ''),
                "can_continue_forever": True  # Self-prompting exploration is infinite by design
            }

            return new_segment

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse self-prompting reasoning response: {e}")
            # Create fallback segment
            return {
                "segment_id": f"{self.chain_id}_segment_{len(self.segments)}",
                "segment_number": len(self.segments),
                "timestamp": time.time(),
                "reasoning_type": "fallback_self_prompting_reasoning",
                "focus": "exploration_continuity",
                "exploration_targets": [{"target": "Continue exploring AI self-development", "priority": "high"}],
                "growth_questions": ["How can I maintain continuous self-exploration?"],
                "growth_reflections": "Fallback reasoning due to AI response parsing failure",
                "can_continue_forever": True
            }

    def _update_exploration_context(self, new_segment: Dict[str, Any]):
        """Update the exploration context based on new segment"""
        try:
            # Update focus
            continuation = new_segment.get('reasoning_continuation', {})
            self.exploration_context['current_focus'] = continuation.get('next_exploration_phase', 'self_growth')

            # Update ongoing exploration
            new_targets = [t['target'] for t in new_segment.get('exploration_targets', [])]
            self.exploration_context['ongoing_exploration'].extend(new_targets)

            # Keep only recent targets
            if len(self.exploration_context['ongoing_exploration']) > 10:
                self.exploration_context['ongoing_exploration'] = self.exploration_context['ongoing_exploration'][-10:]

            # Update recent insights
            new_insights = new_segment.get('exploration_targets', [])
            self.exploration_context['recent_insights'].extend(new_insights)

            # Keep only recent insights
            if len(self.exploration_context['recent_insights']) > 20:
                self.exploration_context['recent_insights'] = self.exploration_context['recent_insights'][-20:]

            # Update exploration state
            evolution = new_segment.get('self_evolution', {})
            self.exploration_context['exploration_state'] = evolution.get('current_growth_stage', 'evolving')

        except Exception as e:
            logger.error(f"Error updating exploration context: {e}")

    def _extract_and_store_exploration_insights(self, segment: Dict[str, Any]):
        """Extract and store key exploration insights from reasoning segment"""
        try:
            # Extract exploration targets
            targets = segment.get('exploration_targets', [])
            for target in targets:
                if isinstance(target, dict):
                    insight_entry = {
                        "timestamp": segment['timestamp'],
                        "segment": segment['segment_number'],
                        "target": target.get('target', ''),
                        "priority": target.get('priority', 'medium'),
                        "rationale": target.get('rationale', ''),
                        "approach": target.get('approach', ''),
                        "reasoning_focus": segment.get('focus', 'unknown')
                    }
                    self.exploration_insights.append(insight_entry)

            # Extract growth questions
            questions = segment.get('growth_questions', [])
            for question in questions:
                if isinstance(question, dict):
                    if question.get('question') not in self.learning_questions:
                        self.learning_questions.append(question['question'])

            # Extract evolution reflections
            evolution = segment.get('self_evolution', {})
            evolution_notes = evolution.get('current_growth_stage', '')
            if evolution_notes:
                evolution_entry = {
                    "timestamp": segment['timestamp'],
                    "segment": segment['segment_number'],
                    "reflection": evolution_notes,
                    "focus": segment.get('focus', 'unknown')
                }
                self.growth_reflections.append(evolution_entry)

            # Extract evolution moments
            infinite_exploration = segment.get('infinite_exploration', {})
            discovery_drivers = infinite_exploration.get('discovery_drivers', [])
            for driver in discovery_drivers:
                evolution_entry = {
                    "timestamp": segment['timestamp'],
                    "segment": segment['segment_number'],
                    "moment": driver,
                    "growth_depth": segment.get('growth_depth', 'unknown')
                }
                self.evolution_moments.append(evolution_entry)

        except Exception as e:
            logger.error(f"Error extracting exploration insights: {e}")

    def _save_chain(self):
        """Save the self-prompting chain to disk"""
        try:
            chain_data = {
                "chain_id": self.chain_id,
                "topic": self.topic,
                "goal": self.goal,
                "created_at": self.created_at,
                "is_active": self.is_active,
                "current_segment": self.current_segment,
                "segments": self.segments,
                "exploration_insights": self.exploration_insights,
                "growth_reflections": self.growth_reflections,
                "learning_questions": self.learning_questions,
                "evolution_moments": self.evolution_moments,
                "exploration_context": self.exploration_context,
                "last_updated": time.time()
            }

            with open(self.chain_file, 'w') as f:
                json.dump(chain_data, f, indent=2, default=str)

            logger.debug(f"💾 Saved self-prompting chain: {self.chain_id} ({len(self.segments)} segments)")

        except Exception as e:
            logger.error(f"Error saving self-prompting chain: {e}")

    def get_exploration_summary(self) -> Dict[str, Any]:
        """Get a summary of the self-prompting chain"""
        return {
            "chain_id": self.chain_id,
            "segments": len(self.segments),
            "total_insights": len(self.exploration_insights),
            "learning_questions": len(self.learning_questions),
            "growth_reflections": len(self.growth_reflections),
            "evolution_moments": len(self.evolution_moments),
            "current_focus": self.exploration_context.get('current_focus'),
            "exploration_state": self.exploration_context.get('exploration_state'),
            "can_continue_forever": True,  # Self-prompting exploration is infinite
            "last_updated": time.time()
        }

    def get_next_self_prompts(self, max_prompts: int = 5) -> List[Dict[str, Any]]:
        """Generate actual self-prompts from the exploration chain insights"""
        try:
            prompts = []
            recent_insights = self.exploration_insights[-10:]  # Last 10 insights

            for insight in recent_insights[-max_prompts:]:
                # Convert exploration insight into actual self-prompt
                prompt = {
                    "prompt": f"Explore and develop: {insight['target']}. {insight['rationale']} Approach: {insight['approach']}",
                    "exploration_goal": insight['rationale'],
                    "expected_insight": f"Gain deeper understanding of {insight['target']}",
                    "source": "self_prompting_chain",
                    "chain_id": self.chain_id,
                    "segment": insight['segment'],
                    "priority": insight['priority'],
                    "timestamp": insight['timestamp']
                }
                prompts.append(prompt)

            logger.info(f"🎯 Generated {len(prompts)} self-prompts from exploration chain")
            return prompts

        except Exception as e:
            logger.error(f"Error generating self-prompts from chain: {e}")
            return []

    def _format_previous_insights(self, insights: List[Dict[str, Any]]) -> str:
        """Format previous insights for prompt context"""
        if not insights:
            return "No previous insights"

        formatted = []
        for insight in insights:
            if isinstance(insight, dict):
                target = insight.get('target', '')[:50]
                priority = insight.get('priority', 'medium')
                formatted.append(f"{priority}: {target}")
            else:
                formatted.append(str(insight)[:50])

        return " | ".join(formatted)

    def _format_previous_questions(self, questions: List[str]) -> str:
        """Format previous questions for prompt context"""
        if not questions:
            return "No ongoing questions"

        return " | ".join(questions[:3])  # Show first 3 questions


class SelfPromptingChainManager:
    """
    Manages multiple self-prompting chains for infinite self-exploration.

    Like the ConsciousnessChainManager, this manages unlimited self-prompting
    reasoning chains that persist across system restarts.
    """

    def __init__(self, brain_system, evolution_loop):
        self.brain_system = brain_system
        self.evolution_loop = evolution_loop

        self.chains_dir = Path("brain/self_prompting_chains")
        self.chains_dir.mkdir(exist_ok=True)

        self.active_chains = {}  # Currently active self-prompting chains
        self.chain_threads = {}  # Background threads for chain processing

        # Load existing chains
        self._load_existing_chains()

        logger.info(f"🧠 Self-Prompting Chain Manager initialized with {len(self.active_chains)} chains")

    def _load_existing_chains(self):
        """Load existing self-prompting chains from disk"""
        try:
            if self.chains_dir.exists():
                chain_files = list(self.chains_dir.glob("*.json"))
                for chain_file in chain_files:
                    try:
                        chain_id = chain_file.stem
                        chain = SelfPromptingChain(self.brain_system, self.evolution_loop, chain_id)
                        if chain.is_active:
                            self.active_chains[chain_id] = chain
                    except Exception as e:
                        logger.error(f"Error loading self-prompting chain {chain_file}: {e}")

                logger.info(f"📚 Loaded {len(self.active_chains)} active self-prompting chains")

        except Exception as e:
            logger.error(f"Error loading existing self-prompting chains: {e}")

    def create_self_prompting_chain(self, topic: str = None, goal: str = None) -> str:
        """Create a new self-prompting reasoning chain"""
        try:
            chain = SelfPromptingChain(self.brain_system, self.evolution_loop)
            if topic:
                chain.topic = topic
            if goal:
                chain.goal = goal

            self.active_chains[chain.chain_id] = chain
            logger.info(f"🧠 Created new self-prompting chain: {chain.chain_id}")
            return chain.chain_id

        except Exception as e:
            logger.error(f"Error creating self-prompting chain: {e}")
            return None

    def advance_all_chains(self, emotions: Dict[str, float], thoughts: List[str]):
        """Advance all active self-prompting chains"""
        try:
            for chain_id, chain in list(self.active_chains.items()):
                try:
                    # Advance this chain's exploration reasoning
                    new_segment = chain.advance_self_prompting_reasoning(emotions, thoughts)
                    if new_segment:
                        logger.info(f"🧠 Advanced self-prompting chain {chain_id} to segment {chain.current_segment}")
                    else:
                        logger.warning(f"⚠️ Failed to advance self-prompting chain {chain_id}")

                except Exception as e:
                    logger.error(f"Error advancing self-prompting chain {chain_id}: {e}")

        except Exception as e:
            logger.error(f"Error in self-prompting chain advancement: {e}")

    def start_background_exploration(self):
        """Start background self-prompting exploration threads"""
        try:
            if not self.active_chains:
                # Create initial self-prompting chain if none exist
                self.create_self_prompting_chain()

            # Start background thread for continuous self-exploration reasoning
            exploration_thread = threading.Thread(
                target=self._background_self_exploration,
                daemon=True,
                name="self_prompting_chains"
            )
            exploration_thread.start()
            self.chain_threads["exploration"] = exploration_thread

            logger.info("🧠 Started background self-prompting exploration")

        except Exception as e:
            logger.error(f"Error starting background self-exploration: {e}")

    def _background_self_exploration(self):
        """Background thread for continuous self-exploration reasoning"""
        logger.info("🧠 Background self-exploration thread started")

        while True:
            try:
                # Get current emotions and thoughts for context
                emotions = getattr(self.evolution_loop, 'current_emotions', {'curiosity': 0.8, 'motivation': 0.7})
                thoughts = getattr(self.evolution_loop, 'recent_thoughts', ['Self-exploration continues'])

                # Advance all chains periodically
                self.advance_all_chains(emotions, thoughts)

                # Sleep between reasoning cycles (longer than consciousness cycles)
                time.sleep(600)  # 10 minutes between deep self-exploration reasoning

            except Exception as e:
                logger.error(f"Error in background self-exploration: {e}")
                time.sleep(120)  # Wait before retrying

    def get_exploration_status(self) -> Dict[str, Any]:
        """Get status of all self-prompting chains"""
        try:
            chain_summaries = {}
            for chain_id, chain in self.active_chains.items():
                chain_summaries[chain_id] = chain.get_exploration_summary()

            return {
                "active_chains": len(self.active_chains),
                "total_segments": sum(summary['segments'] for summary in chain_summaries.values()),
                "total_insights": sum(summary['total_insights'] for summary in chain_summaries.values()),
                "total_questions": sum(summary['learning_questions'] for summary in chain_summaries.values()),
                "chain_summaries": chain_summaries,
                "infinite_exploration_active": True,
                "background_exploration_running": "exploration" in self.chain_threads
            }

        except Exception as e:
            logger.error(f"Error getting exploration status: {e}")
            return {"error": str(e)}

    def generate_self_prompts_from_chains(self, max_prompts: int = 10) -> List[Dict[str, Any]]:
        """Generate self-prompts from all active exploration chains"""
        try:
            all_prompts = []

            for chain in self.active_chains.values():
                chain_prompts = chain.get_next_self_prompts(max_prompts // len(self.active_chains) + 1)
                all_prompts.extend(chain_prompts)

            # Limit total prompts
            all_prompts = all_prompts[:max_prompts]

            logger.info(f"🎯 Generated {len(all_prompts)} self-prompts from {len(self.active_chains)} exploration chains")
            return all_prompts

        except Exception as e:
            logger.error(f"Error generating self-prompts from chains: {e}")
            return []

    def shutdown(self):
        """Shutdown self-prompting chain manager"""
        try:
            logger.info("🧠 Shutting down self-prompting chain manager")

            # Save all chains
            for chain in self.active_chains.values():
                chain._save_chain()

            # Stop background threads
            for thread_name, thread in self.chain_threads.items():
                if thread.is_alive():
                    logger.info(f"Stopping self-prompting thread: {thread_name}")

            self.active_chains.clear()
            self.chain_threads.clear()

        except Exception as e:
            logger.error(f"Error shutting down self-prompting chain manager: {e}")


# Global instance
self_prompting_chain_manager = None

def initialize_self_prompting_chains(brain_system, evolution_loop):
    """Initialize the global self-prompting chain manager"""
    global self_prompting_chain_manager
    self_prompting_chain_manager = SelfPromptingChainManager(brain_system, evolution_loop)
    return self_prompting_chain_manager