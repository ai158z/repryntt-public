#!/usr/bin/env python3
"""
Chain-of-Thought Engine — Extracted from SAIGE monolith (Phase 6).

Manages the full lifecycle of Chain-of-Thought (CoT) explorations:
  - CoT queue management (load, save, queue, dequeue)
  - Chain creation (regular + self-autonomous with PoA action plans)
  - Chain advancement with synthesis, milestone tracking, conclusion gating
  - Phase management (exploration → selection → specification → output)
  - Context compression for long-running chains
  - Exploration history and topic repetition prevention

Dependencies injected via ``__init__``:
  brain_system  — the monolith BrainSystem (provides personality_brain,
                  brain_path, prompt_generator, consciousness, lock, etc.)

Once Phase 8 replaces BrainSystem with a composed repryntt-native class,
each dependency will be a first-class repryntt module instead.
"""

import json
import logging
import os
import re
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Cross-platform file locking
try:
    import fcntl
except ImportError:
    fcntl = None

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase configuration — maps phase names to step ranges & goals
# ---------------------------------------------------------------------------
CHAIN_PHASES = {
    "exploration": {
        "name": "Divergent Exploration",
        "step_range": (1, 15),
        "goal": "Generate diverse ideas and explore multiple angles",
        "requirements": [
            "At least 3 distinct concepts",
            "Multiple domain perspectives",
            "Identify knowledge gaps",
        ],
        "transition_criteria": "15 steps completed OR sufficient concept diversity detected",
    },
    "selection": {
        "name": "Convergent Selection",
        "step_range": (16, 25),
        "goal": "Evaluate concepts and select the best 1-2 for deep dive",
        "requirements": [
            "Evaluation criteria defined",
            "Scored comparison of top concepts",
            "Clear justification for selection",
            "ONE concept chosen",
        ],
        "transition_criteria": "One concept selected with >80% justification score",
    },
    "specification": {
        "name": "Technical Specification",
        "step_range": (26, 50),
        "goal": "Engineer detailed specifications with real numbers",
        "requirements": [
            "At least 5 quantitative estimates",
            "3+ material/tech references",
            "Constraint analysis",
            "Component breakdown",
        ],
        "transition_criteria": "All major components specified with metrics",
    },
    "output": {
        "name": "Actionable Output",
        "step_range": (51, 69),
        "goal": "Create implementation roadmap OR research paper",
        "requirements": [
            "Step-by-step plan OR formal paper structure",
            "Resource requirements",
            "Validation approach",
        ],
        "transition_criteria": "Complete actionable document produced",
    },
}


class ChainOfThoughtEngine:
    """Full CoT lifecycle manager — extracted from BrainSystem."""

    # Expose phase config as class attribute so callers can inspect it
    CHAIN_PHASES = CHAIN_PHASES

    def __init__(self, brain_system):
        self.brain = brain_system

        # Convenience aliases — avoids long self.brain.xxx chains
        self._brain_path: Path = Path(brain_system.brain_path)
        self._chains_dir: Path = self._brain_path / "chains"
        self._chains_dir.mkdir(parents=True, exist_ok=True)

        # CoT queue state
        self.cot_queue: List[Dict] = []
        self.cot_queue_file: Path = self._brain_path / "cot_queue.json"
        self.cot_queue_lock = threading.Lock()

        # AI-initiated chain queue
        self.ai_chain_queue: List[Dict] = []
        self.ai_chain_queue_file: Path = self._brain_path / "ai_chain_queue.json"

        # Load queues on init
        self._load_cot_queue()
        self._load_ai_chain_queue()

    # ===================================================================== #
    #  Property helpers for brain_system attributes                          #
    # ===================================================================== #
    @property
    def personality_brain(self) -> Dict:
        return self.brain.personality_brain

    @property
    def personality_brain_path(self) -> Path:
        return Path(self.brain.personality_brain_path)

    @property
    def consciousness(self):
        return getattr(self.brain, "consciousness", None)

    @property
    def prompt_generator(self):
        return getattr(self.brain, "prompt_generator", None)

    @property
    def synthesis_engine(self):
        return getattr(self.brain, "synthesis_engine", None)

    @property
    def conclusion_evaluator(self):
        return getattr(self.brain, "conclusion_evaluator", None)

    @property
    def output_processor(self):
        return getattr(self.brain, "output_processor", None)

    @property
    def task_hierarchy(self):
        return getattr(self.brain, "task_hierarchy", None)

    @property
    def lock(self):
        return self.brain.lock

    # ===================================================================== #
    #  1. COT QUEUE MANAGEMENT                                              #
    # ===================================================================== #

    def _load_cot_queue(self):
        """Load the COT queue from database or persistent storage and clean up completed topics."""
        try:
            if getattr(self.brain, "use_database", False):
                try:
                    db = self.brain._get_db_session()
                    if db:
                        from repryntt.database.models import BrainMemory
                        cot_memory = db.query(BrainMemory).filter_by(
                            memory_id="cot_queue", memory_type="system"
                        ).first()
                        if cot_memory:
                            self.cot_queue = json.loads(cot_memory.content)
                            logger.info(f"📋 Loaded {len(self.cot_queue)} queued COTs from database")
                            original_count = len(self.cot_queue)
                            self._cleanup_completed_queued_cots()
                            if len(self.cot_queue) < original_count:
                                logger.info(f"🧹 Removed {original_count - len(self.cot_queue)} queued COTs for completed chains")
                                self._save_cot_queue()
                            return
                except Exception as e:
                    logger.warning(f"Database load failed for COT queue, falling back to JSON: {e}")

            if self.cot_queue_file.exists():
                with open(self.cot_queue_file, "r") as f:
                    self.cot_queue = json.load(f)
                logger.info(f"📋 Loaded {len(self.cot_queue)} queued COTs from persistent storage")
                original_count = len(self.cot_queue)
                self._cleanup_completed_queued_cots()
                if len(self.cot_queue) < original_count:
                    logger.info(f"🧹 Removed {original_count - len(self.cot_queue)} queued COTs for completed chains")
                    with open(self.cot_queue_file, "w") as f:
                        json.dump(self.cot_queue, f, indent=2, default=str)
            else:
                self.cot_queue = []
                logger.info("📋 Initialized empty COT queue")
        except Exception as e:
            logger.error(f"Error loading COT queue: {e}")
            self.cot_queue = []

    def _cleanup_completed_queued_cots(self):
        """Remove queued COTs that reference completed chains."""
        try:
            if not self._chains_dir.exists():
                return
            completed_topics = set()
            for chain_file in self._chains_dir.glob("*.json"):
                try:
                    with open(chain_file, "r", encoding="utf-8") as f:
                        chain_data = json.load(f)
                    chain_status = chain_data.get("status", "").lower()
                    if "complete" in chain_status or chain_data.get("goal_achieved", False):
                        topic = chain_data.get("metadata", {}).get("topic", "")
                        if topic:
                            completed_topics.add(topic)
                except Exception as e:
                    logger.debug(f"Error reading chain file {chain_file}: {e}")
            if completed_topics:
                original_queue = self.cot_queue[:]
                self.cot_queue = [
                    cot for cot in self.cot_queue if cot.get("topic") not in completed_topics
                ]
                if len(self.cot_queue) < len(original_queue):
                    removed = [c.get("topic") for c in original_queue if c not in self.cot_queue]
                    logger.info(f"🧹 Cleaned up queued COTs for completed topics: {', '.join(removed[:3])}...")
        except Exception as e:
            logger.error(f"Error cleaning up completed queued COTs: {e}")

    def _save_cot_queue(self):
        """Save the COT queue to database and JSON."""
        try:
            with self.cot_queue_lock:
                if getattr(self.brain, "use_database", False):
                    try:
                        db = self.brain._get_db_session()
                        if db:
                            from repryntt.database.models import BrainMemory
                            existing = db.query(BrainMemory).filter_by(
                                memory_id="cot_queue", memory_type="system"
                            ).first()
                            if existing:
                                existing.content = json.dumps(self.cot_queue)
                                existing.last_accessed = datetime.utcnow()
                            else:
                                db.add(BrainMemory(
                                    memory_id="cot_queue",
                                    memory_type="system",
                                    content=json.dumps(self.cot_queue),
                                    importance=1.0,
                                ))
                            db.commit()
                    except Exception as e:
                        logger.warning(f"Database save failed for COT queue: {e}")
                with open(self.cot_queue_file, "w") as f:
                    json.dump(self.cot_queue, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving COT queue: {e}")

    def queue_chain_of_thought(self, topic: str, goal: str, priority: int = 0, requested_by: str = "user") -> str:
        """Queue a user-requested chain of thought."""
        try:
            queue_item = {
                "id": f"queued_cot_{int(time.time())}_{hash(topic + goal) % 10000}",
                "topic": topic,
                "goal": goal,
                "priority": priority,
                "requested_by": requested_by,
                "queued_at": time.time(),
                "status": "queued",
            }
            with self.cot_queue_lock:
                self.cot_queue.append(queue_item)
                self.cot_queue.sort(key=lambda x: (-x["priority"], x["queued_at"]))
            self._save_cot_queue()
            position = len([
                item for item in self.cot_queue
                if item["priority"] >= priority and item["queued_at"] <= queue_item["queued_at"]
            ])
            logger.info(f"📋 Queued COT '{topic}' (priority: {priority}, position: {position})")
            return f"✅ Queued chain of thought: '{topic}' (position: {position} in queue)"
        except Exception as e:
            logger.error(f"Error queuing COT: {e}")
            return f"X Failed to queue chain of thought: {e}"

    def get_next_queued_cot(self) -> Optional[Dict[str, Any]]:
        """Get the next queued COT to process (removes it from queue)."""
        try:
            with self.cot_queue_lock:
                if not self.cot_queue:
                    return None
                next_cot = self.cot_queue.pop(0)
                self._save_cot_queue()
                logger.info(f"🎯 Retrieved queued COT: '{next_cot['topic']}'")
                return next_cot
        except Exception as e:
            logger.error(f"Error getting next queued COT: {e}")
            return None

    def get_cot_queue_status(self) -> Dict[str, Any]:
        """Get current COT queue status."""
        try:
            with self.cot_queue_lock:
                return {
                    "total_queued": len(self.cot_queue),
                    "high_priority": len([i for i in self.cot_queue if i["priority"] >= 2]),
                    "normal_priority": len([i for i in self.cot_queue if i["priority"] == 1]),
                    "low_priority": len([i for i in self.cot_queue if i["priority"] <= 0]),
                    "queue_items": self.cot_queue[:5],
                }
        except Exception as e:
            logger.error(f"Error getting COT queue status: {e}")
            return {"error": str(e)}

    def clear_cot_queue(self) -> str:
        """Clear all queued COTs."""
        try:
            with self.cot_queue_lock:
                cleared_count = len(self.cot_queue)
                self.cot_queue = []
            self._save_cot_queue()
            logger.info(f"🧹 Cleared {cleared_count} queued COTs")
            return f"✅ Cleared {cleared_count} queued chains of thought"
        except Exception as e:
            logger.error(f"Error clearing COT queue: {e}")
            return f"X Failed to clear COT queue: {e}"

    # ===================================================================== #
    #  2. AI CHAIN QUEUE                                                    #
    # ===================================================================== #

    def _load_ai_chain_queue(self):
        """Load the AI chain queue from disk."""
        try:
            if self.ai_chain_queue_file.exists():
                with open(self.ai_chain_queue_file, "r") as f:
                    self.ai_chain_queue = json.load(f)
                logger.info(f"📋 Loaded {len(self.ai_chain_queue)} queued AI chains")
            else:
                self.ai_chain_queue = []
        except Exception as e:
            logger.error(f"Error loading AI chain queue: {e}")
            self.ai_chain_queue = []

    def _save_ai_chain_queue(self):
        """Save the AI chain queue to disk."""
        try:
            with open(self.ai_chain_queue_file, "w") as f:
                json.dump(self.ai_chain_queue, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving AI chain queue: {e}")

    def _queue_ai_chain(self, topic, goal, initial_prompt, milestones=None, success_criteria=None):
        """Add a chain to the AI-initiated queue."""
        queue_entry = {
            "topic": topic,
            "goal": goal,
            "initial_prompt": initial_prompt,
            "milestones": milestones,
            "success_criteria": success_criteria,
            "queued_at": time.time(),
            "status": "queued",
        }
        self.ai_chain_queue.append(queue_entry)
        self._save_ai_chain_queue()
        logger.info(f"📋 Queued AI chain: '{topic}' - Queue size: {len(self.ai_chain_queue)}")

    def _check_and_start_next_queued_chain(self):
        """Check if there are queued chains and start the next one if possible."""
        try:
            active_chains = self.personality_brain.get("active_chains_of_thought", [])
            active_count = sum(1 for c in active_chains if c.get("status") == "active")
            if active_count > 0 or not self.ai_chain_queue:
                return
            next_chain = self.ai_chain_queue.pop(0)
            self._save_ai_chain_queue()
            logger.info(f"🚀 Starting queued AI chain: '{next_chain['topic']}' - {len(self.ai_chain_queue)} remaining")
            result = self.create_chain_of_thought(
                topic=next_chain["topic"],
                goal=next_chain["goal"],
                initial_prompt=next_chain["initial_prompt"],
                milestones=next_chain.get("milestones"),
                success_criteria=next_chain.get("success_criteria"),
            )
            if isinstance(result, str) and "⏳" in result:
                logger.warning("Chain was re-queued, this shouldn't happen")
            else:
                logger.info(f"✅ Successfully started queued chain: '{next_chain['topic']}'")
        except Exception as e:
            logger.error(f"Error starting queued chain: {e}")

    def get_ai_chain_queue_status(self) -> Dict[str, Any]:
        """Get the current status of the AI chain queue."""
        return {
            "queue_size": len(self.ai_chain_queue),
            "queued_chains": [
                {
                    "topic": c["topic"],
                    "goal": c["goal"],
                    "queued_at": c["queued_at"],
                    "waiting_time": time.time() - c["queued_at"],
                }
                for c in self.ai_chain_queue
            ],
        }

    # ===================================================================== #
    #  3. CHAIN LIFECYCLE — CLEANUP / STUCK DETECTION                       #
    # ===================================================================== #

    def _cleanup_stale_active_chains(self):
        """Clean up and synchronize active chains list with actual chain files on startup."""
        try:
            if "active_chains_of_thought" not in self.personality_brain:
                self.personality_brain["active_chains_of_thought"] = []

            active_chains = self.personality_brain["active_chains_of_thought"]
            cleaned_chains = []
            removed_count = 0
            added_count = 0

            for chain_info in active_chains:
                chain_id = chain_info.get("chain_id")
                if not chain_id:
                    removed_count += 1
                    logger.warning(f"🧹 Removed entry without chain_id: {chain_info.get('topic', 'N/A')[:50]}...")
                    continue
                chain_file = self._chains_dir / f"{chain_id}.json"
                try:
                    if chain_file.exists():
                        with open(chain_file, "r") as f:
                            chain_data = json.load(f)
                        if chain_data.get("goal_achieved", False):
                            removed_count += 1
                            logger.info(f"🧹 Removed completed chain {chain_id} on startup")
                            continue
                        if self._is_chain_stuck(chain_data):
                            chain_info["status"] = "inactive"
                            chain_data["metadata"]["status"] = "inactive"
                            with open(chain_file, "w", encoding="utf-8") as f:
                                json.dump(chain_data, f, indent=2, ensure_ascii=False)
                            removed_count += 1
                            logger.warning(f"🧹 Marked stuck chain {chain_id} as inactive")
                        else:
                            cleaned_chains.append(chain_info)
                    else:
                        removed_count += 1
                        logger.warning(f"🧹 Removed non-existent chain {chain_id} on startup")
                except Exception as e:
                    logger.warning(f"⚠️ Could not check chain {chain_id}: {e}")
                    cleaned_chains.append(chain_info)

            # First-time setup: add missing self-autonomous chains
            if not cleaned_chains:
                logger.info("📝 No active chains — performing first-time synchronization")
                for chain_file in self._chains_dir.glob("*.json"):
                    try:
                        chain_id = chain_file.stem
                        with open(chain_file, "r") as f:
                            chain_data = json.load(f)
                        metadata = chain_data.get("metadata", {}) or {}
                        if chain_data.get("goal_achieved", False):
                            continue
                        is_autonomous = metadata.get("autonomous", False)
                        chain_type = metadata.get("chain_type")
                        if not (is_autonomous or chain_type == "self_autonomous"):
                            continue
                        cleaned_chains.append({
                            "chain_id": chain_id,
                            "topic": metadata.get("topic", "Unknown"),
                            "goal": metadata.get("goal", ""),
                            "status": "active",
                            "created_at": metadata.get("created_at", time.time()),
                            "autonomous": metadata.get("autonomous", False),
                        })
                        added_count += 1
                        logger.info(f"📝 Added chain to active list: {metadata.get('topic', 'Unknown')[:50]}...")
                    except Exception as e:
                        logger.warning(f"⚠️ Could not check chain {chain_file.name}: {e}")

            # Second pass: fix metadata status in chain files
            for chain_file in self._chains_dir.glob("*.json"):
                try:
                    with open(chain_file, "r") as f:
                        chain_data = json.load(f)
                    metadata = chain_data.get("metadata", {})
                    if metadata.get("status") == "inactive":
                        continue
                    should_inactive = False
                    if chain_data.get("goal_achieved", False):
                        should_inactive = True
                    elif self._is_chain_stuck(chain_data):
                        should_inactive = True
                    if should_inactive:
                        chain_data["metadata"]["status"] = "inactive"
                        with open(chain_file, "w", encoding="utf-8") as f:
                            json.dump(chain_data, f, indent=2, ensure_ascii=False)
                except Exception as e:
                    logger.warning(f"⚠️ Could not update chain file {chain_file.name}: {e}")

            self.personality_brain["active_chains_of_thought"] = cleaned_chains
            if removed_count > 0 or added_count > 0:
                logger.info(f"🧹 Active chains sync: removed {removed_count}, added {added_count}")
                with open(self.personality_brain_path, "w") as f:
                    json.dump(self.personality_brain, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error synchronizing active chains: {e}")

    def _is_chain_stuck(self, chain_data: Dict) -> bool:
        """Check if a chain is stuck and should be marked as inactive."""
        try:
            metadata = chain_data.get("metadata", {})
            goal = metadata.get("goal", "")
            if "AI_SERVICE_ERROR" in goal or "timed out" in goal or "timeout" in goal:
                return True
            conclusion = chain_data.get("conclusion")
            if conclusion and conclusion != "null" and len(str(conclusion).strip()) > 10:
                return True
            mc = chain_data.get("milestone_completion", {})
            if mc and all(mc.values()) and not chain_data.get("goal_achieved", False):
                return True
            created_at = metadata.get("created_at", 0)
            last_step_time = 0
            seq = chain_data.get("chain_sequence", [])
            if seq:
                last_step_time = seq[-1].get("timestamp", 0)
            now = time.time()
            if (now - created_at) > 86400 and (now - last_step_time) > 43200:
                return True
            empty = sum(1 for s in seq[-5:] if len(s.get("response", "").strip()) < 10)
            if empty >= 3:
                return True
            return False
        except Exception as e:
            logger.error(f"Error checking if chain is stuck: {e}")
            return False

    # ===================================================================== #
    #  4. PHASE MANAGEMENT                                                  #
    # ===================================================================== #

    def _determine_chain_phase(self, chain_data: Dict[str, Any]) -> str:
        """Determine current phase based on step count and chain progress."""
        try:
            current_step = len(chain_data.get("chain_sequence", []))
            if "current_phase" in chain_data.get("metadata", {}):
                current_phase = chain_data["metadata"]["current_phase"]
                phase_config = self.CHAIN_PHASES.get(current_phase, {})
                step_range = phase_config.get("step_range", (1, 69))
                if step_range[0] <= current_step <= step_range[1]:
                    return current_phase
            if current_step <= 15:
                return "exploration"
            elif current_step <= 25:
                return "selection"
            elif current_step <= 50:
                return "specification"
            else:
                return "output"
        except Exception as e:
            logger.error(f"Error determining chain phase: {e}")
            return "exploration"

    def _should_transition_phase(self, chain_data: Dict[str, Any], current_phase: str) -> Tuple[bool, Optional[str]]:
        """Check if chain should transition to next phase."""
        try:
            current_step = len(chain_data.get("chain_sequence", []))
            phase_config = self.CHAIN_PHASES.get(current_phase, {})
            step_range = phase_config.get("step_range", (1, 69))
            if current_step > step_range[1]:
                phase_order = ["exploration", "selection", "specification", "output"]
                current_idx = phase_order.index(current_phase) if current_phase in phase_order else 0
                if current_idx < len(phase_order) - 1:
                    return True, phase_order[current_idx + 1]
                return True, "output"
            if current_phase == "exploration" and current_step >= 15:
                concepts = self._extract_key_concepts(chain_data)
                if len(concepts) >= 3:
                    return True, "selection"
            elif current_phase == "selection" and current_step >= 20:
                if self._has_selected_concept(chain_data):
                    return True, "specification"
            elif current_phase == "specification" and current_step >= 40:
                if self._has_sufficient_specifications(chain_data):
                    return True, "output"
            elif current_phase == "output":
                if self._should_complete_output_phase(chain_data):
                    return True, "completed"
            return False, None
        except Exception as e:
            logger.error(f"Error checking phase transition: {e}")
            return False, None

    def _should_complete_output_phase(self, chain_data: Dict[str, Any]) -> bool:
        """Check if output phase has generated sufficient content."""
        try:
            output_steps = [s for s in chain_data.get("chain_sequence", []) if s.get("phase") == "output"]
            if len(output_steps) < 3:
                return False
            task_type_str = chain_data.get("metadata", {}).get("task_type", "research_analysis")
            try:
                if isinstance(task_type_str, str):
                    task_type_str = task_type_str.lower().replace(" ", "_")
                from repryntt.routing.task_hierarchy import TaskType
                task_type = TaskType(task_type_str)
                task_config = self.task_hierarchy.get_task_config(task_type)
            except (ValueError, KeyError, AttributeError, ImportError):
                logger.warning(f"Unknown task type '{task_type_str}', falling back to research analysis")
                from repryntt.routing.task_hierarchy import TaskType
                task_config = self.task_hierarchy.get_task_config(TaskType.RESEARCH_ANALYSIS)

            recent_responses = [s.get("response", "") for s in output_steps[-3:]]
            if self._responses_are_too_similar(recent_responses):
                logger.info("🔄 Output phase showing repetition — marking as complete")
                return True

            last_response = output_steps[-1].get("response", "").lower()
            success_score = 0
            for criterion in task_config.evaluation_criteria:
                if criterion.lower() in last_response:
                    success_score += 1
            for metric in task_config.success_metrics:
                if metric.lower().replace(" ", "_") in last_response or metric.lower() in last_response:
                    success_score += 2

            min_score = max(3, len(task_config.evaluation_criteria) // 2)
            if success_score >= min_score and len(output_steps) >= 3:
                logger.info(f"✅ Output phase meets {task_config.name} criteria (score: {success_score}/{min_score})")
                return True

            from repryntt.routing.task_hierarchy import TaskType as TT
            max_steps = 8 if task_config.task_type == TT.CREATIVE_WRITING else 6
            if len(output_steps) >= max_steps:
                logger.warning(f"⚠️ Output phase reached {max_steps} steps — forcing completion")
                return True
            return False
        except Exception as e:
            logger.error(f"Error checking output phase completion: {e}")
            return len([s for s in chain_data.get("chain_sequence", []) if s.get("phase") == "output"]) >= 8

    def _generate_phase_prompt(self, chain_data: Dict[str, Any], phase: str) -> str:
        """Generate phase-specific continuation prompt with task-aware templates."""
        try:
            current_step = len(chain_data.get("chain_sequence", []))
            phase_config = self.CHAIN_PHASES.get(phase, {})
            topic = chain_data["metadata"]["topic"]
            goal = chain_data["metadata"]["goal"]

            task_description = f"{topic} {goal}"
            task_config = self.task_hierarchy.classify_task(task_description)

            if "task_type" not in chain_data["metadata"]:
                chain_data["metadata"]["task_type"] = task_config.task_type.value
                chain_data["metadata"]["task_config"] = task_config.to_dict()
                logger.info(f"📋 Chain classified as: {task_config.name} task type")

            recent_steps = chain_data.get("chain_sequence", [])[-3:]
            recent_summary = "\n".join([f"Step {s['step']}: {s['response'][:200]}..." for s in recent_steps])
            explored_concepts = self._extract_key_concepts(chain_data)

            base_prompt = f"""CHAIN-OF-THOUGHT CONTINUATION

TOPIC: {topic}
GOAL: {goal}

CURRENT PHASE: {phase_config.get('name', phase)} (Step {current_step})
PHASE GOAL: {phase_config.get('goal', '')}

RECENT PROGRESS:
{recent_summary}

CONCEPTS ALREADY EXPLORED (avoid exact repetition):
{', '.join(explored_concepts[:10])}

"""
            task_prompt_template = task_config.prompt_templates.get(phase, "")
            if task_prompt_template:
                enhanced_template = task_prompt_template
                if phase in ["specification", "output"] and current_step > 5:
                    enhanced_template += (
                        "\n\nCREATION REQUIREMENTS (MANDATORY):\n"
                        "• BUILD and IMPLEMENT at least one working solution\n"
                        "• CREATE functional prototypes or designs\n"
                        "• DEVELOP practical applications, not just theories\n"
                        "• DELIVER tangible results that can be used or tested"
                    )
                prompt = base_prompt + enhanced_template.format(topic=topic)
            else:
                prompt = base_prompt + self._generic_phase_prompt(phase, chain_data, topic)
            return prompt
        except Exception as e:
            logger.error(f"Error generating phase prompt: {e}")
            return f"Continuing chain: {chain_data['metadata']['topic']}"

    def _generic_phase_prompt(self, phase: str, chain_data: Dict, topic: str) -> str:
        """Generate generic phase prompts when no task-specific template exists."""
        if phase == "exploration":
            return (
                "EXPLORATION PHASE REQUIREMENTS:\n"
                "- Generate diverse ideas across multiple domains\n"
                "- Explore at least 3 distinct conceptual angles\n"
                "- Identify knowledge gaps and unknowns\n"
                "- Consider unconventional approaches\n"
                "- Ask 'what if?' questions\n\n"
                "YOUR TASK: Propose a NEW angle or concept that hasn't been deeply explored yet."
            )
        elif phase == "selection":
            return (
                "SELECTION PHASE REQUIREMENTS:\n"
                "- Evaluate top 3 concepts from exploration phase\n"
                "- Define clear evaluation criteria (feasibility, impact, novelty, resources)\n"
                "- Score each concept objectively\n"
                "- SELECT ONE concept with detailed justification\n"
                "- Explain why other concepts were rejected\n\n"
                "YOUR TASK: If concepts haven't been evaluated yet, list and score them. "
                "If evaluation is complete, make your FINAL SELECTION with justification."
            )
        elif phase == "specification":
            return (
                "SPECIFICATION PHASE REQUIREMENTS:\n"
                "- Provide QUANTITATIVE estimates (numbers with units)\n"
                "- Reference specific materials, technologies, or methods\n"
                "- Break down into components/subsystems\n"
                "- Identify constraints and limitations\n"
                "- Include at least 3 numerical metrics\n\n"
                "YOUR TASK: Add technical specifications with REAL NUMBERS."
            )
        elif phase == "output":
            is_impl = self._is_currently_implementable(chain_data)
            if is_impl:
                return (
                    "CREATE DETAILED IMPLEMENTATION ROADMAP:\n\n"
                    "PHASE 1: PLANNING (Weeks 1-2)\n"
                    "PHASE 2: RESOURCE ACQUISITION (Weeks 3-4)\n"
                    "PHASE 3: DEVELOPMENT (Weeks 5-12)\n"
                    "PHASE 4: TESTING & VALIDATION (Weeks 13-14)\n"
                    "PHASE 5: DEPLOYMENT & MAINTENANCE (Week 15+)\n\n"
                    "PROVIDE CONCRETE NUMBERS: timelines, costs, team sizes, metrics."
                )
            else:
                return (
                    "STRUCTURE AS FORMAL RESEARCH PAPER:\n\n"
                    "1. TITLE  2. ABSTRACT  3. INTRODUCTION  4. METHODOLOGY\n"
                    "5. RESULTS & FINDINGS  6. DISCUSSION  7. CONCLUSION\n\n"
                    "FOCUS: Ensure academic rigor, cite specific evidence, avoid speculation."
                )
        return f"Continuing chain: {topic}"

    # ===================================================================== #
    #  5. CHAIN CREATION                                                    #
    # ===================================================================== #

    def create_chain_of_thought(self, topic: str, goal: str, initial_prompt: str,
                                milestones: List[str] = None, success_criteria: List[str] = None,
                                target_steps: int = None) -> str:
        """Create a new chain-of-thought JSON file for focused exploration."""
        with self.lock:
            try:
                # Credit system check
                try:
                    from repryntt.trading.robot_economy import get_ai_wallet_address
                    ai_wallet = get_ai_wallet_address(self.brain)
                    rem = getattr(self.brain, "robot_economy_manager", None)
                    if rem:
                        bal = rem.get_wallet_balance(ai_wallet)
                        if bal.get("success") and bal.get("balance_credits", 0) < 0.05:
                            return f"X Insufficient credits to create chain"
                except Exception:
                    pass

                active_chains = self.personality_brain.get("active_chains_of_thought", [])
                active_count = sum(1 for c in active_chains if c.get("status") == "active")
                if active_count > 0:
                    logger.info(f"📋 Queueing chain for topic '{topic}' — {active_count} active chain(s)")
                    self._queue_ai_chain(topic, goal, initial_prompt, milestones, success_criteria)
                    return f"⏳ Chain queued — will start after current chain completes. Queue position: {len(self.ai_chain_queue)}"

                chain_id = f"chain_{int(time.time())}_{hash(topic) % 10000}"
                chain_file_path = self._chains_dir / f"{chain_id}.json"

                if not milestones:
                    milestones = self._generate_chain_milestones(topic, goal)
                if not success_criteria:
                    success_criteria = self._generate_success_criteria(topic, goal)

                chain_data = {
                    "metadata": {
                        "chain_id": chain_id,
                        "topic": topic,
                        "goal": goal,
                        "created_at": time.time(),
                        "status": "active",
                        "progress_level": 0.0,
                        "current_phase": "exploration",
                        "phase_history": [],
                        "milestones": milestones,
                        "success_criteria": success_criteria,
                        "expected_duration_steps": target_steps if target_steps else len(milestones) + 2,
                        "chain_type": self._classify_chain_type(topic, goal),
                    },
                    "chain_sequence": [{
                        "step": 1,
                        "timestamp": time.time(),
                        "prompt": initial_prompt,
                        "response": "",
                        "insights": [],
                        "next_questions": [],
                        "milestone_progress": [],
                    }],
                    "overall_insights": [],
                    "conclusion": None,
                    "goal_achieved": False,
                    "milestone_completion": {m: False for m in milestones},
                }

                with open(chain_file_path, "w") as f:
                    json.dump(chain_data, f, indent=2, default=str)

                active_chains = self.personality_brain.get("active_chains_of_thought", [])
                for c in active_chains:
                    c["status"] = "inactive"
                active_chains.append({
                    "chain_id": chain_id, "topic": topic, "goal": goal,
                    "file_path": str(chain_file_path),
                    "created_at": time.time(), "last_updated": time.time(),
                    "status": "active",
                })
                self.personality_brain["active_chains_of_thought"] = active_chains[-5:]
                with open(self.personality_brain_path, "w") as f:
                    json.dump(self.personality_brain, f, indent=2, default=str)

                if self.consciousness:
                    self.consciousness.send_signal("cot_started", {"chain_id": chain_id, "topic": topic, "goal": goal})
                return f"✅ Created chain-of-thought: {chain_id} for topic '{topic}'"
            except Exception as e:
                logger.error(f"Error creating chain of thought: {e}")
                return f"X Error creating chain of thought: {str(e)}"

    def create_chain_of_thought_data(self, topic: str, goal: str, initial_prompt: str,
                                     milestones: List[str] = None, success_criteria: List[str] = None,
                                     target_steps: int = None) -> Optional[Dict[str, Any]]:
        """Create a new chain-of-thought and return the chain data dict (for internal use)."""
        with self.lock:
            try:
                active_chains = self.personality_brain.get("active_chains_of_thought", [])
                active_count = sum(1 for c in active_chains if c.get("status") == "active")
                if active_count > 0:
                    logger.warning(f"🚫 Skipping chain data creation for '{topic}' — {active_count} active chains")
                    return None

                chain_id = f"chain_{int(time.time())}_{hash(topic) % 10000}"
                chain_file_path = self._chains_dir / f"{chain_id}.json"

                if not milestones:
                    milestones = self._generate_chain_milestones(topic, goal)
                if not success_criteria:
                    success_criteria = self._generate_success_criteria(topic, goal)

                chain_data = {
                    "metadata": {
                        "chain_id": chain_id, "topic": topic, "goal": goal,
                        "created_at": time.time(), "status": "active",
                        "progress_level": 0.0, "current_phase": "exploration",
                        "phase_history": [], "milestones": milestones,
                        "success_criteria": success_criteria,
                        "expected_duration_steps": target_steps if target_steps else len(milestones) + 2,
                        "chain_type": self._classify_chain_type(topic, goal),
                    },
                    "chain_sequence": [{
                        "step": 1, "timestamp": time.time(), "prompt": initial_prompt,
                        "response": "", "insights": [], "next_questions": [], "milestone_progress": [],
                    }],
                    "overall_insights": [], "conclusion": None, "goal_achieved": False,
                    "milestone_completion": {m: False for m in milestones},
                }

                with open(chain_file_path, "w") as f:
                    json.dump(chain_data, f, indent=2, default=str)

                active_chains = self.personality_brain.get("active_chains_of_thought", [])
                for c in active_chains:
                    c["status"] = "inactive"
                active_chains.append({
                    "chain_id": chain_id, "topic": topic, "goal": goal,
                    "file_path": str(chain_file_path),
                    "created_at": time.time(), "last_updated": time.time(), "status": "active",
                })
                self.personality_brain["active_chains_of_thought"] = active_chains[-5:]
                with open(self.personality_brain_path, "w") as f:
                    json.dump(self.personality_brain, f, indent=2, default=str)

                logger.info(f"✅ Created chain-of-thought data: {chain_id}")
                self._check_and_start_next_queued_chain()
                return chain_data
            except Exception as e:
                logger.error(f"Error creating chain of thought data: {e}")
                return None

    def create_self_autonomous_chain(self, topic: str, goal: str,
                                     task_type: str = "auto", target_steps: int = None) -> str:
        """Create a SELF-AUTONOMOUS chain-of-thought with PoA action plans."""
        with self.lock:
            try:
                active_chains = self.personality_brain.get("active_chains_of_thought", [])
                if self._check_topic_similarity(topic, active_chains):
                    logger.warning(f"🚫 Topic similarity blocked creation: '{topic}'")
                    return ""

                active_count = sum(1 for c in active_chains if c.get("status") == "active")
                if active_count > 0:
                    # Safety valve: force-conclude stale chains
                    for chain in active_chains:
                        if chain.get("status") != "active":
                            continue
                        cid = chain.get("chain_id", "")
                        cf = self._chains_dir / f"{cid}.json"
                        if cf.exists():
                            try:
                                with open(cf, "r") as f:
                                    cd = json.load(f)
                                step_count = len(cd.get("chain_sequence", []))
                                max_steps = cd.get("metadata", {}).get("expected_duration_steps", 15)
                                created_at = cd.get("metadata", {}).get("created_at", 0)
                                if isinstance(created_at, str):
                                    try:
                                        created_at = datetime.fromisoformat(created_at).timestamp()
                                    except Exception:
                                        created_at = 0
                                age = time.time() - created_at if created_at else 9999
                                stale = step_count >= max_steps or age > 1800
                                if stale:
                                    reason = f"step limit ({step_count}/{max_steps})" if step_count >= max_steps else f"stale ({age:.0f}s)"
                                    logger.info(f"🔄 Force-concluding {cid} ({reason})")
                                    cd["goal_achieved"] = True
                                    cd["metadata"]["status"] = "completed"
                                    cd["conclusion"] = f"Force-concluded: {reason}"
                                    with open(cf, "w") as f2:
                                        json.dump(cd, f2, indent=2, default=str)
                                    chain["status"] = "completed"
                                    active_count -= 1
                            except Exception as e:
                                logger.warning(f"Error checking chain age: {e}")
                        else:
                            chain["status"] = "completed"
                            active_count -= 1
                    if active_count <= 0:
                        self.personality_brain["active_chains_of_thought"] = active_chains
                        self._save_personality_brain()
                    if active_count > 0:
                        logger.warning(f"🚫 Skipping creation for '{topic}' — {active_count} active chains")
                        return ""

                chain_id = f"chain_{int(time.time())}_{hash(topic) % 10000}"
                chain_file_path = self._chains_dir / f"{chain_id}.json"

                short_goal = self.prompt_generator._shorten_goal(goal) if self.prompt_generator else goal[:120]

                if task_type in ("auto", "creative_writing", "", None):
                    try:
                        task_desc = f"{topic} {goal}"
                        task_config = self.task_hierarchy.classify_task(task_desc)
                        task_type = task_config.task_type.value
                        logger.info(f"🎯 Auto-classified: '{task_type}' for '{topic}'")
                    except Exception as e:
                        logger.warning(f"⚠️ Task classification failed: {e}")
                        task_type = "research_analysis"

                num_steps = target_steps if target_steps else self._estimate_steps_for_task(task_type, goal)
                action_plan = self._generate_action_plan(goal, task_type, num_steps)
                milestones = action_plan if action_plan else [
                    "Establish exploration foundation", "Develop initial insights",
                    "Connect insights into patterns", "Reach meaningful conclusions",
                ]
                success_criteria = [
                    "Tools were actually used (not just described)",
                    "Concrete deliverable produced (file, analysis, code)",
                    "Evidence-based conclusion with specific findings",
                ]

                preliminary = {
                    "metadata": {"expected_duration_steps": num_steps, "task_type": task_type, "action_plan": action_plan},
                    "chain_sequence": [],
                }
                initial_prompt = self.prompt_generator.generate_next_step_prompt(
                    chain_context={"insights": [], "progress_level": 0.0},
                    current_insights=[], goal=goal, chain_data=preliminary,
                ) if self.prompt_generator else f"Explore: {goal}"

                logger.info(f"📋 PoA: Generated {len(action_plan)}-step plan for '{topic}' (type: {task_type})")

                chain_data = {
                    "metadata": {
                        "chain_id": chain_id, "topic": topic, "goal": short_goal,
                        "full_goal": goal, "created_at": time.time(), "status": "active",
                        "progress_level": 0.0, "current_phase": "execution",
                        "phase_history": [], "milestones": milestones,
                        "success_criteria": success_criteria, "action_plan": action_plan,
                        "expected_duration_steps": num_steps, "chain_type": "self_autonomous",
                        "task_type": task_type,
                        "autonomous_flags": {
                            "ai_generated_prompts": True,
                            "ai_driven_conclusions": True,
                            "pipeline_of_actions": True,
                        },
                    },
                    "chain_sequence": [{
                        "step": 1, "timestamp": time.time(), "prompt": initial_prompt,
                        "response": "", "insights": [], "next_questions": [],
                        "milestone_progress": [],
                        "synthesis": {"insights": [], "connections": [], "synthesis": "", "contribution_to_conclusion": 0},
                    }],
                    "overall_insights": [], "conclusion": None, "goal_achieved": False,
                    "milestone_completion": {m: False for m in milestones},
                }

                with open(chain_file_path, "w", encoding="utf-8") as f:
                    json.dump(chain_data, f, indent=2, ensure_ascii=False)

                if "active_chains_of_thought" not in self.personality_brain:
                    self.personality_brain["active_chains_of_thought"] = []
                for c in self.personality_brain["active_chains_of_thought"]:
                    c["status"] = "inactive"
                self.personality_brain["active_chains_of_thought"].append({
                    "chain_id": chain_id, "topic": topic, "goal": short_goal,
                    "status": "active", "created_at": time.time(), "autonomous": True,
                })
                with open(self.personality_brain_path, "w") as f:
                    json.dump(self.personality_brain, f, indent=2, default=str)

                logger.info(f"✅ Created SELF-AUTONOMOUS chain: {chain_id}")
                return chain_id
            except Exception as e:
                logger.error(f"X Failed to create self-autonomous chain: {e}")
                return ""

    # ===================================================================== #
    #  6. CHAIN ADVANCEMENT                                                 #
    # ===================================================================== #

    def update_chain_progress(self, chain_id: str, response: str,
                              insights: List[str] = None, next_questions: List[str] = None,
                              conclusion: str = None) -> str:
        """Update progress in an active chain-of-thought with phased prompting."""
        try:
            active_chains = self.personality_brain.get("active_chains_of_thought", [])
            chain_info = None
            for chain in active_chains:
                if chain["chain_id"] == chain_id:
                    chain_info = chain
                    break
            if not chain_info:
                return f"X Chain '{chain_id}' not found in active chains"

            chain_file_path = self._chains_dir / f"{chain_id}.json"
            if not chain_file_path.exists():
                chain_file_path = self._brain_path / f"{chain_id}.json"

            with open(chain_file_path, "r") as f:
                chain_data = json.load(f)

            current_phase = self._determine_chain_phase(chain_data)
            if "current_phase" not in chain_data.get("metadata", {}):
                chain_data["metadata"]["current_phase"] = current_phase
                chain_data["metadata"]["phase_history"] = []

            is_repetitive = self._detect_concept_repetition(chain_data, response, threshold=0.7)
            if is_repetitive:
                logger.warning(f"⚠️ Detected high concept repetition in chain {chain_id}")

            filtered_response = self._filter_response_content(response)
            next_prompt = self._generate_phase_prompt(chain_data, current_phase)

            current_step = len(chain_data["chain_sequence"])
            new_step = {
                "step": current_step + 1, "timestamp": time.time(),
                "prompt": next_prompt, "response": filtered_response,
                "insights": insights or [], "next_questions": next_questions or [],
                "phase": current_phase,
            }
            chain_data["chain_sequence"].append(new_step)

            if insights:
                chain_data["overall_insights"].extend(insights)

            should_transition, next_phase = self._should_transition_phase(chain_data, current_phase)
            if should_transition and next_phase:
                if next_phase == "completed":
                    logger.info(f"✅ Chain {chain_id} output phase completed")
                    chain_data["metadata"]["status"] = "completed"
                    chain_data["goal_achieved"] = True
                    chain_data["completion_timestamp"] = time.time()
                    chain_data["completion_reason"] = "output phase generated sufficient content"
                    chain_data["conclusion"] = "Chain completed successfully with actionable output."
                else:
                    logger.info(f"🔄 Chain {chain_id}: {current_phase} → {next_phase}")
                    chain_data["metadata"]["current_phase"] = next_phase
                    chain_data["metadata"]["phase_history"].append({
                        "from_phase": current_phase, "to_phase": next_phase,
                        "step": current_step + 1, "timestamp": time.time(),
                    })

            if conclusion:
                chain_data["conclusion"] = conclusion
                chain_data["goal_achieved"] = True
                chain_data["metadata"]["status"] = "completed"
                chain_data["completion_timestamp"] = time.time()
                chain_data["completion_reason"] = "explicit conclusion provided"
                self._check_and_start_next_queued_chain()

            total_steps = len(chain_data["chain_sequence"])
            chain_data["metadata"]["progress_level"] = min(1.0, total_steps / 69.0)

            if total_steps >= 69 and not chain_data.get("goal_achieved"):
                chain_data["goal_achieved"] = False
                chain_data["metadata"]["status"] = "completed"
                chain_data["completion_timestamp"] = time.time()
                chain_data["completion_reason"] = "maximum steps reached"
                cp = chain_data["metadata"].get("current_phase", "unknown")
                if cp == "output":
                    chain_data["conclusion"] = "Chain completed through all phases."
                else:
                    chain_data["conclusion"] = f"Chain reached max steps in {cp} phase."
                if "active_chains_of_thought" in self.personality_brain:
                    self.personality_brain["active_chains_of_thought"] = [
                        c for c in self.personality_brain["active_chains_of_thought"]
                        if c.get("chain_id") != chain_id
                    ]
                self._check_and_start_next_queued_chain()

            with open(chain_file_path, "w") as f:
                json.dump(chain_data, f, indent=2, default=str)

            self._update_milestone_progress(chain_data, response, insights or [])

            # Credit reward for completion
            if chain_data.get("goal_achieved"):
                try:
                    from repryntt.trading.robot_economy import get_ai_wallet_address
                    rem = getattr(self.brain, "robot_economy_manager", None)
                    if rem:
                        ai_wallet = get_ai_wallet_address(self.brain)
                        rem.reward_ai_for_task(ai_wallet, 0.2, f"chain_completion_{chain_id}")
                        logger.info(f"💰 Rewarded 0.2 CR for chain completion: {chain_id}")
                except Exception:
                    pass

            chain_info["last_updated"] = time.time()
            with open(self.personality_brain_path, "w") as f:
                json.dump(self.personality_brain, f, indent=2, default=str)

            if self.consciousness:
                self.consciousness.send_signal("cot_step", {
                    "chain_id": chain_id, "step": current_step + 1, "insights": insights or [],
                })
                if chain_data.get("goal_achieved") or chain_data["metadata"]["status"] == "completed":
                    self.consciousness.send_signal("cot_completed", {
                        "chain_id": chain_id, "conclusion": chain_data.get("conclusion"),
                    })

            status = chain_data["metadata"]["status"]
            phase_info = f"Phase: {chain_data['metadata'].get('current_phase', 'unknown')}"
            return f"✅ Updated chain '{chain_id}' — Step {current_step + 1}, {phase_info}, Status: {status}"
        except Exception as e:
            logger.error(f"Error updating chain progress: {e}")
            return f"X Error updating chain progress: {str(e)}"

    def advance_self_autonomous_chain(self, chain_id: str, step_output: str,
                                      tool_results: Dict[str, Any] = None) -> Dict[str, Any]:
        """Advance a self-autonomous chain with file-level locking."""
        try:
            chain_file_path = self._chains_dir / f"{chain_id}.json"
            lock_file_path = self._chains_dir / f"{chain_id}.lock"

            lock_fd = open(lock_file_path, "w")
            try:
                if fcntl:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif hasattr(__builtins__, '__import__'):
                    import msvcrt
                    msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
            except BlockingIOError:
                logger.info(f"🔒 Chain {chain_id} locked — skipping")
                lock_fd.close()
                return {"should_continue": True, "next_prompt": None, "skipped": True, "reason": "chain_locked"}

            try:
                with open(chain_file_path, "r", encoding="utf-8") as f:
                    chain_data = json.load(f)

                self._validate_and_fix_step_numbers(chain_data)

                if chain_data.get("status", "").lower() == "completed" or chain_data.get("goal_achieved", False):
                    logger.warning(f"🚫 Chain {chain_id} already completed — rejecting advance")
                    return {"should_continue": False, "next_prompt": None, "error": "Chain already completed"}

                current_step = len(chain_data["chain_sequence"])
                chain_data["chain_sequence"][current_step - 1]["response"] = step_output

                if tool_results and tool_results.get("tool_calls_executed"):
                    tool_execution_results = tool_results
                else:
                    tool_execution_results = {"tool_calls_executed": [], "tool_calls_failed": [], "insights_summary": []}
                chain_data["chain_sequence"][current_step - 1]["tool_results"] = tool_execution_results

                synthesis_result = self.synthesis_engine.synthesize_step(step_output, {
                    "insights": chain_data.get("overall_insights", []),
                    "progress_level": chain_data["metadata"]["progress_level"],
                    "goal": chain_data["metadata"]["goal"],
                })
                chain_data["chain_sequence"][current_step - 1]["synthesis"] = synthesis_result

                if synthesis_result["insights"]:
                    chain_data["overall_insights"].extend(synthesis_result["insights"])

                self._update_milestone_progress(chain_data, step_output, synthesis_result["insights"])

                # Conclusion detection
                parsed_output = self.output_processor.process(step_output, context="chain_step")
                expected_steps = chain_data["metadata"].get("expected_duration_steps", 15)

                if parsed_output.chain_complete:
                    should_conclude = True
                    logger.info(f"🎯 AI self-concluded chain {chain_id}")
                elif current_step >= expected_steps:
                    should_conclude = True
                    logger.info(f"🔴 Step limit reached: {current_step}/{expected_steps}")
                else:
                    should_conclude = self.conclusion_evaluator.should_conclude(
                        chain_context={
                            "insights": chain_data["overall_insights"],
                            "progress_level": chain_data["metadata"]["progress_level"],
                            "goal": chain_data["metadata"]["goal"],
                        },
                        synthesis_result=synthesis_result,
                    )

                # PoA conclusion gating
                if should_conclude and current_step < expected_steps:
                    if not self._verify_chain_deliverables(chain_data):
                        should_conclude = False
                        action_plan = chain_data["metadata"].get("action_plan", [])
                        short_goal = chain_data["metadata"].get("goal", "the task")
                        action_plan.append(
                            f"PRODUCE DELIVERABLE NOW: Use write_file or store_learning for: {short_goal[:80]}"
                        )
                        chain_data["metadata"]["action_plan"] = action_plan
                        chain_data["metadata"]["expected_duration_steps"] = max(expected_steps, current_step + 2)
                        logger.warning(f"⚠️ CONCLUSION GATED for {chain_id}: no deliverables verified")

                if should_conclude:
                    conclusion = self._generate_self_autonomous_conclusion(chain_data, synthesis_result)
                    chain_data["conclusion"] = conclusion
                    chain_data["goal_achieved"] = True
                    chain_data["metadata"]["status"] = "completed"
                    chain_data["metadata"]["progress_level"] = 1.0

                    self._save_completed_cot_topic(chain_data["metadata"]["topic"])

                    if "active_chains_of_thought" in self.personality_brain:
                        self.personality_brain["active_chains_of_thought"] = [
                            c for c in self.personality_brain["active_chains_of_thought"]
                            if c.get("chain_id") != chain_id
                        ]
                    with open(self.personality_brain_path, "w") as f:
                        json.dump(self.personality_brain, f, indent=2, default=str)

                    self._safe_save_chain(chain_file_path, chain_data)

                    # Auto-export PDF for creative/research chains
                    task_type = chain_data["metadata"].get("task_type", "creative_writing")
                    if task_type in ["creative_writing", "research", "analysis"]:
                        try:
                            from cot_to_academic_paper import create_academic_paper
                            pdf_path = create_academic_paper(chain_file_path)
                            logger.info(f"✅ Research paper exported to: {pdf_path}")
                        except Exception as e:
                            logger.warning(f"⚠️ Could not auto-export PDF: {e}")

                    return {"should_continue": False, "next_prompt": None, "conclusion": conclusion, "synthesis": synthesis_result}
                else:
                    # Generate next prompt
                    all_insights = chain_data["overall_insights"]
                    recent = all_insights[-10:] if len(all_insights) > 10 else all_insights
                    if len(all_insights) > 10:
                        summary = f"[Previous {len(all_insights)-10} insights summarized]"
                        recent.insert(0, summary)

                    next_prompt = self.prompt_generator.generate_next_step_prompt(
                        chain_context={"insights": recent, "progress_level": chain_data["metadata"]["progress_level"]},
                        current_insights=synthesis_result["insights"],
                        goal=chain_data["metadata"]["goal"],
                        chain_data=chain_data,
                    ) if self.prompt_generator else f"Continue exploring: {chain_data['metadata']['goal']}"

                    next_step = {
                        "step": current_step + 1, "timestamp": time.time(),
                        "prompt": next_prompt, "response": "", "tool_results": None,
                        "insights": [], "next_questions": [], "milestone_progress": [],
                        "synthesis": {"insights": [], "connections": [], "synthesis": "", "contribution_to_conclusion": 0},
                    }
                    chain_data["chain_sequence"].append(next_step)
                    chain_data["metadata"]["progress_level"] = min(
                        chain_data["metadata"]["progress_level"] + 0.1, 0.9
                    )
                    self._safe_save_chain(chain_file_path, chain_data)

                    return {"should_continue": True, "next_prompt": next_prompt, "conclusion": None, "synthesis": synthesis_result}
            finally:
                if fcntl:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()
        except Exception as e:
            logger.error(f"X Failed to advance self-autonomous chain: {e}")
            return {"should_continue": False, "next_prompt": None, "conclusion": f"Chain advancement failed: {e}", "synthesis": {}}

    def _safe_save_chain(self, chain_file_path: Path, chain_data: Dict):
        """Save chain with fallback to default=str."""
        try:
            with open(chain_file_path, "w", encoding="utf-8") as f:
                json.dump(chain_data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"X Failed to save chain: {e}")
            try:
                with open(chain_file_path, "w", encoding="utf-8") as f:
                    json.dump(chain_data, f, indent=2, default=str)
                logger.warning("⚠️ Saved chain with default=str fallback")
            except Exception as e2:
                logger.error(f"X Failed even with fallback: {e2}")

    # ===================================================================== #
    #  7. CONCLUSION GENERATION & CONTEXT COMPRESSION                       #
    # ===================================================================== #

    def _generate_self_autonomous_conclusion(self, chain_data: Dict, final_synthesis: Dict) -> str:
        """Generate a conclusion based on AI-synthesized insights with context compression."""
        task_type = chain_data["metadata"].get("task_type", "creative_writing")
        topic = chain_data["metadata"]["topic"]
        goal = chain_data["metadata"]["goal"]
        total_steps = len(chain_data["chain_sequence"])
        all_insights = chain_data.get("overall_insights", [])
        compressed = self._compress_chain_insights_for_conclusion(all_insights, topic)

        if task_type in ["creative_writing", "research", "analysis"]:
            themes_text = ""
            if compressed.get("themes"):
                lines = []
                for theme in compressed["themes"][:3]:
                    name = str(theme.get("name", "Unknown")).replace('"', "").replace("'", "")
                    summary = str(theme.get("summary", "")).replace('"', "").replace("'", "")
                    lines.append(f"- {name}: {summary}")
                themes_text = "\n".join(lines)
            safe_topic = str(topic).replace('"', "").replace("'", "")
            safe_goal = str(goal).replace('"', "").replace("'", "")
            safe_summary = str(compressed.get("summary", "")).replace('"', "").replace("'", "")
            conclusion_prompt = (
                f"You have completed a {total_steps}-step exploration of: {safe_topic}\n\n"
                f"GOAL: {safe_goal}\nSUMMARY: {safe_summary}\n\n"
                f"KEY THEMES:\n{themes_text}\n\n"
                "Write a concise conclusion (300-500 words) summarizing the main findings."
            )
        else:
            conclusion_prompt = (
                f"Generate a focused conclusion for this exploration.\n\n"
                f"TOPIC: {topic}\nGOAL: {goal}\n"
                f"SUMMARY: {compressed['summary']}\nTOTAL INSIGHTS: {len(all_insights)}\n\n"
                "Generate a conclusion capturing key insights and implications."
            )

        try:
            call_ai = getattr(self.brain, "_call_ai_service", None)
            if call_ai:
                orig_bc = getattr(self.brain, "use_blockchain_ai", False)
                orig_pct = getattr(self.brain, "blockchain_ai_percentage", 0)
                self.brain.use_blockchain_ai = False
                self.brain.blockchain_ai_percentage = 0
                try:
                    est_tokens = len(conclusion_prompt) // 4
                    if est_tokens > 1200:
                        safe_summary_t = safe_summary[:800] if len(safe_summary) > 800 else safe_summary
                        conclusion_prompt = (
                            f"You completed a {total_steps}-step exploration of: {safe_topic}\n\n"
                            f"GOAL: {safe_goal}\nSUMMARY: {safe_summary_t}\n\n"
                            "Write a concise conclusion (200-400 words)."
                        )
                    conclusion = call_ai(conclusion_prompt, timeout=180, include_tools=False)
                    if conclusion and "AI_SERVICE_ERROR" in conclusion:
                        raise RuntimeError(conclusion)
                    return conclusion
                except Exception as api_err:
                    err_str = str(api_err).lower()
                    if "context" in err_str or "ai_service_error" in str(api_err):
                        logger.warning("Context overflow in conclusion — trying ultra-compressed")
                        safe_summary_short = str(compressed.get("summary", ""))[:400]
                        ultra = f'Conclude exploration of "{topic}" ({len(all_insights)} insights).\nSummary: {safe_summary_short}\nWrite a 150-250 word conclusion.'
                        try:
                            result = call_ai(ultra, timeout=120, include_tools=False)
                            if result and "AI_SERVICE_ERROR" not in result:
                                return result
                        except Exception:
                            pass
                        return self._generate_fallback_conclusion_from_compressed_data(compressed, topic, goal, total_steps)
                    raise
                finally:
                    self.brain.use_blockchain_ai = orig_bc
                    self.brain.blockchain_ai_percentage = orig_pct
            else:
                return f"Exploration completed with {len(all_insights)} key insights."
        except Exception as e:
            logger.error(f"Error generating conclusion: {e}")
            return f"Self-autonomous exploration concluded with {len(all_insights)} insights generated."

    def _compress_chain_insights_for_conclusion(self, insights: List[str], topic: str) -> Dict[str, Any]:
        """Compress chain insights into thematic summaries for conclusion generation."""
        try:
            if not insights:
                return {"summary": f"No insights developed during exploration of {topic}", "themes": []}

            theme_kw = {
                "technical": ["technology", "implementation", "architecture", "system", "framework", "tool"],
                "scientific": ["research", "study", "analysis", "methodology", "experiment", "data"],
                "practical": ["application", "solution", "implementation", "deployment", "usage"],
                "theoretical": ["theory", "concept", "understanding", "framework", "model"],
                "future": ["future", "emerging", "trend", "development", "innovation", "advancement"],
                "challenges": ["challenge", "problem", "issue", "limitation", "difficulty", "obstacle"],
            }
            themes: Dict[str, List[str]] = {}
            for insight in insights:
                low = insight.lower()
                best, best_sc = "general", 0
                for name, kws in theme_kw.items():
                    sc = sum(1 for k in kws if k in low)
                    if sc > best_sc:
                        best, best_sc = name, sc
                themes.setdefault(best, []).append(insight)

            compressed = []
            for name, items in themes.items():
                compressed.append({
                    "name": name.replace("_", " ").title(),
                    "count": len(items),
                    "summary": f"{len(items)} insights about {name.replace('_', ' ')} aspects",
                    "key_insights": items[:3],
                })

            total = len(insights)
            tc = len(compressed)
            if tc == 1:
                summary = f"Exploration developed {total} insights focused on {compressed[0]['name'].lower()} aspects of {topic}"
            else:
                names = [t["name"].lower() for t in compressed[:3]]
                summary = f"Exploration developed {total} insights across {tc} themes: {', '.join(names)}"
            return {"summary": summary, "themes": compressed, "total_insights": total}
        except Exception as e:
            logger.warning(f"Error compressing insights: {e}")
            return {
                "summary": f"Exploration of {topic} developed {len(insights)} key insights",
                "themes": [{"name": "General Findings", "count": len(insights), "summary": f"{len(insights)} insights developed"}],
                "total_insights": len(insights),
            }

    def _generate_fallback_conclusion_from_compressed_data(self, compressed: Dict, topic: str, goal: str, total_steps: int) -> str:
        """Generate a conclusion from compressed data when AI calls fail."""
        try:
            summary = str(compressed.get("summary", f"Exploration of {topic}"))
            themes = compressed.get("themes", [])
            total_insights = compressed.get("total_insights", 0)

            parts = [
                f"# {topic}: Exploration Conclusion\n\n",
                f"## Overview\n",
                f"This {total_steps}-step exploration developed {total_insights} key insights addressing: {goal}\n\n",
                f"## Key Findings\n{summary}\n\n",
            ]
            if themes:
                parts.append("## Thematic Summary\n")
                for t in themes[:3]:
                    parts.append(f"- **{t['name']}**: {t['summary']}\n")
                parts.append("\n")
            parts.extend([
                "## Implications\nThe insights provide valuable understanding for future exploration.\n\n",
                "## Conclusion\n",
                f"This exploration of {topic} demonstrates systematic AI-driven investigation value.",
            ])
            return "".join(parts)
        except Exception as e:
            logger.error(f"Error generating fallback conclusion: {e}")
            return f"Exploration of '{topic}' completed with {total_steps} steps."

    def _compress_old_chain_steps(self, chain_data: Dict) -> None:
        """Compress old chain steps to prevent context overflow."""
        try:
            seq = chain_data.get("chain_sequence", [])
            if len(seq) <= 20:
                return
            early = seq[:10]
            middle_summary = self._create_chain_steps_summary(seq[10:20], "middle_exploration")
            chain_data["chain_sequence"] = early + [middle_summary] + seq[-10:]
            logger.info(f"🗜️ Compressed chain: {len(seq)} → {len(chain_data['chain_sequence'])} entries")
        except Exception as e:
            logger.warning(f"Error compressing chain steps: {e}")

    def _create_chain_steps_summary(self, steps: List[Dict], phase_name: str) -> Dict:
        """Create a summary entry for compressed chain steps."""
        try:
            total_steps = len(steps)
            total_insights = sum(len(s.get("insights", [])) for s in steps)
            total_tools = sum(len(s.get("tool_results", {}).get("tool_calls_executed", []))
                             for s in steps if isinstance(s.get("tool_results"), dict))
            responses = [s.get("response", "") for s in steps if s.get("response")]
            key_themes = self._extract_key_themes_from_responses(responses)

            return {
                "timestamp": steps[-1].get("timestamp", time.time()) if steps else time.time(),
                "response": (
                    f"COMPRESSED EXPLORATION PHASE ({phase_name}):\n"
                    f"• Completed {total_steps} steps\n"
                    f"• Developed {total_insights} insights across: {', '.join(key_themes[:3])}\n"
                    f"• Used {total_tools} tools for research"
                ),
                "tool_results": {"compressed": True, "original_steps": total_steps},
                "insights": [], "next_questions": [],
                "milestone_progress": ["Connect insights into patterns"],
                "synthesis": {"compressed": True},
                "compressed": True,
            }
        except Exception as e:
            logger.warning(f"Error creating steps summary: {e}")
            return {"timestamp": time.time(), "response": f"COMPRESSED: {len(steps)} steps in {phase_name}", "compressed": True}

    # ===================================================================== #
    #  8. CONCLUSION EVALUATION & GOAL CHECKING                             #
    # ===================================================================== #

    def prompt_ai_conclusion_evaluation(self, chain_id: str) -> Dict[str, Any]:
        """Prompt the AI to evaluate whether the chain should conclude."""
        try:
            chain_file = self._chains_dir / f"{chain_id}.json"
            if not chain_file.exists():
                return {"should_conclude": False, "reasoning": f"Chain {chain_id} not found", "next_steps": [], "confidence": 0.0}

            with open(chain_file, "r") as f:
                chain_data = json.load(f)

            metadata = chain_data.get("metadata", {})
            goal = metadata.get("goal", "Unknown")
            topic = metadata.get("topic", "Unknown")
            progress = chain_data.get("progress_level", 0)
            insights = chain_data.get("overall_insights", [])
            seq = chain_data.get("chain_sequence", [])

            recent_steps = seq[-5:] if len(seq) > 5 else seq
            recent_responses = [s.get("response", "") for s in recent_steps]
            recent_insights = insights[-10:] if len(insights) > 10 else insights

            evaluation_prompt = (
                "Evaluate whether this exploration should conclude.\n\n"
                f"Topic: {topic}\nGoal: {goal}\nProgress: {progress * 100:.1f}%\n"
                f"Insights: {len(insights)}\nSteps: {len(seq)}\n\n"
                f"RECENT ACTIVITY:\n"
                + "\n".join([f"Step {i+1}: {r[:200]}..." for i, r in enumerate(recent_responses)])
                + "\n\nKEY INSIGHTS:\n"
                + "\n".join([f"• {ins}" for ins in recent_insights[-5:]])
                + "\n\nRESPONSE FORMAT:\nCONCLUDE_OR_CONTINUE: [CONCLUDE/CONTINUE]\n"
                "CONFIDENCE: [0.0-1.0]\nREASONING: [explanation]\nNEXT_STEPS: [suggestions]"
            )

            call_ai = getattr(self.brain, "_call_ai_service", None)
            if not call_ai:
                return {"should_conclude": False, "reasoning": "AI service unavailable", "next_steps": [], "confidence": 0.0}

            orig_bc = getattr(self.brain, "use_blockchain_ai", False)
            orig_pct = getattr(self.brain, "blockchain_ai_percentage", 0)
            self.brain.use_blockchain_ai = False
            self.brain.blockchain_ai_percentage = 0
            try:
                ai_response = call_ai(evaluation_prompt, timeout=300, include_tools=True)
            finally:
                self.brain.use_blockchain_ai = orig_bc
                self.brain.blockchain_ai_percentage = orig_pct

            if ai_response and ai_response.startswith("AI_SERVICE_ERROR"):
                return {"should_conclude": False, "reasoning": ai_response, "next_steps": [], "confidence": 0.0}

            should_conclude = "CONCLUDE" in ai_response.upper()
            confidence = 0.5
            m = re.search(r"CONFIDENCE:\s*([0-9.]+)", ai_response, re.IGNORECASE)
            if m:
                try:
                    confidence = min(1.0, max(0.0, float(m.group(1))))
                except Exception:
                    pass
            reasoning = "AI evaluation completed"
            m = re.search(r"REASONING:\s*(.+?)(?=NEXT_STEPS:|$)", ai_response, re.IGNORECASE | re.DOTALL)
            if m:
                reasoning = m.group(1).strip()
            next_steps = []
            m = re.search(r"NEXT_STEPS:\s*(.+)$", ai_response, re.IGNORECASE | re.DOTALL)
            if m:
                steps = re.split(r"[•\-\*\d]+\.?\s*", m.group(1))
                next_steps = [s.strip() for s in steps if s.strip() and len(s.strip()) > 10]

            return {
                "should_conclude": should_conclude, "reasoning": reasoning,
                "next_steps": next_steps[:5], "confidence": confidence,
                "ai_response": ai_response,
            }
        except Exception as e:
            logger.error(f"Error in AI conclusion evaluation: {e}")
            return {"should_conclude": False, "reasoning": str(e), "next_steps": [], "confidence": 0.0}

    def _check_chain_goal_achievement(self, chain_data: Dict, response: str, insights: List[str]) -> bool:
        """Check if a chain's goal has been achieved."""
        try:
            goal = chain_data["metadata"]["goal"].lower()
            response_lower = response.lower()
            insights_text = " ".join(insights).lower()

            mc = chain_data.get("milestone_completion", {})
            completed = sum(1 for v in mc.values() if v)
            if mc and completed >= len(mc) * 0.8:
                return True

            achievement_kw = [
                "achieved", "completed", "solved", "developed", "created", "built",
                "understood", "learned", "mastered", "accomplished", "finished",
                "concluded", "resolved", "answered", "determined",
            ]
            for kw in achievement_kw:
                if kw in response_lower and any(w in response_lower for w in ["goal", "objective", "task", "exploration"]):
                    return True

            sc = chain_data["metadata"].get("success_criteria", [])
            matches = 0
            for criterion in sc:
                words = criterion.lower().split()[:4]
                if any(w in response_lower or w in insights_text for w in words):
                    matches += 1
            if sc and matches >= len(sc) * 0.7:
                return True

            if len(chain_data["overall_insights"]) >= 5 and insights:
                if any(p in insights_text for p in ["therefore", "thus", "consequently", "in conclusion", "finally"]):
                    return True
            return False
        except Exception as e:
            logger.error(f"Error checking goal achievement: {e}")
            return False

    def _generate_chain_conclusion(self, chain_data: Dict, final_response: str) -> str:
        """Generate a meaningful conclusion for a completed chain."""
        try:
            topic = chain_data["metadata"]["topic"]
            goal = chain_data["metadata"]["goal"]
            ins = chain_data["overall_insights"][-3:]
            parts = [
                f"Exploration of '{topic}' has reached a meaningful conclusion.",
                f"The goal to '{goal}' has been addressed.",
            ]
            if ins:
                parts.append(f"Key insights: {'; '.join(ins[:2])}")
            parts.append("This provides a foundation for future work.")
            return " ".join(parts)
        except Exception as e:
            logger.error(f"Error generating conclusion: {e}")
            return f"Chain exploration of '{chain_data['metadata']['topic']}' completed."

    def _verify_chain_deliverables(self, chain_data: Dict) -> bool:
        """Verify that the chain produced tangible deliverables (conclusion gating)."""
        steps = chain_data.get("chain_sequence", [])
        if not steps:
            return False
        tools_count = 0
        substance_count = 0
        for step in steps:
            tr = step.get("tool_results", {})
            if tr and (tr.get("tool_calls_executed") or tr.get("results")):
                tools_count += 1
            resp = step.get("response", "").strip()
            if resp and len(resp) > 100:
                if not (resp.startswith("TOOL_CALL:") or resp.startswith('{"tool_name"') or resp.startswith('{"tool"')):
                    substance_count += 1
        total = len(steps)
        has_tools = tools_count >= max(1, total * 0.25)
        has_substance = substance_count >= max(1, total * 0.3)
        verified = has_tools or has_substance
        if not verified:
            logger.warning(f"⚠️ Deliverable verification FAILED: {tools_count}/{total} tools, {substance_count}/{total} substance")
        return verified

    # ===================================================================== #
    #  9. TOPIC SIMILARITY & COMPLETED TOPICS                               #
    # ===================================================================== #

    def _check_topic_similarity(self, new_topic: str, min_similarity: float = 0.3) -> Optional[Dict]:
        """Check if a topic is too similar to a recently completed one (adaptive threshold)."""
        try:
            completed = self._load_completed_cot_topics()
            if not completed:
                return None
            best_match = None
            best_overlap = 0.0
            for entry in completed:
                old_topic = entry.get("topic", "")
                overlap = self._calculate_topic_overlap(new_topic, old_topic)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_match = entry
            recent = [e for e in completed
                      if time.time() - e.get("completed_at", 0) < 86400]
            recent_count = len(recent)
            adaptive_threshold = min_similarity
            if recent_count > 3:
                adaptive_threshold = max(0.2, min_similarity - 0.1)
            elif recent_count < 2:
                adaptive_threshold = min(0.5, min_similarity + 0.1)

            if best_overlap >= adaptive_threshold and best_match:
                return {
                    "similar_topic": best_match.get("topic", "Unknown"),
                    "overlap": best_overlap,
                    "completed_at": best_match.get("completed_at", 0),
                    "threshold_used": adaptive_threshold,
                }
            return None
        except Exception as e:
            logger.warning(f"Error checking topic similarity: {e}")
            return None

    def _calculate_topic_overlap(self, topic_a: str, topic_b: str) -> float:
        """Calculate word overlap between two topics (Jaccard)."""
        words_a = set(topic_a.lower().split())
        words_b = set(topic_b.lower().split())
        if not words_a or not words_b:
            return 0.0
        return len(words_a & words_b) / len(words_a | words_b)

    def _load_completed_cot_topics(self) -> List[Dict]:
        """Load list of completed chain-of-thought topics."""
        try:
            path = self.brain_path / "completed_cot_topics.json"
            if path.exists():
                with open(path, "r") as f:
                    data = json.load(f)
                return data if isinstance(data, list) else []
        except Exception as e:
            logger.warning(f"Error loading completed topics: {e}")
        return []

    def _save_completed_cot_topic(self, topic: str, chain_id: str, insights_count: int = 0) -> None:
        """Save a completed chain-of-thought topic."""
        try:
            topics = self._load_completed_cot_topics()
            topics.append({
                "topic": topic, "chain_id": chain_id,
                "completed_at": time.time(), "insights_count": insights_count,
            })
            topics = topics[-100:]
            path = self.brain_path / "completed_cot_topics.json"
            with open(path, "w") as f:
                json.dump(topics, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving completed topic: {e}")

    # ===================================================================== #
    #  10. MILESTONES & CLASSIFICATIONS                                     #
    # ===================================================================== #

    def _generate_chain_milestones(self, task_type: str, topic: str, total_steps: int) -> List[str]:
        """Generate milestone list based on task type."""
        templates = {
            "research": ["Identify 3+ unique research angles", "Gather evidence for main hypothesis",
                         "Analyze 5+ sources or data points", "Form preliminary conclusions",
                         "Synthesize findings into coherent narrative"],
            "creative_writing": ["Develop unique creative concept", "Build core narrative structure",
                                 "Create compelling imagery/metaphors", "Refine and polish expression",
                                 "Complete final creative piece"],
            "analysis": ["Define analytical framework", "Gather relevant data points",
                         "Identify key patterns and trends", "Draw evidence-based conclusions",
                         "Formulate actionable recommendations"],
            "problem_solving": ["Define the problem clearly", "Identify root causes",
                                "Generate 3+ potential solutions", "Evaluate solutions against criteria",
                                "Select and detail the best approach"],
            "learning": ["Map current knowledge boundaries", "Identify 3+ learning objectives",
                         "Explore core concepts in depth", "Connect new knowledge to existing",
                         "Demonstrate understanding through synthesis"],
        }
        return templates.get(task_type, templates["research"])[:max(3, total_steps // 3)]

    def _generate_success_criteria(self, task_type: str, topic: str, goal: str) -> List[str]:
        """Generate success criteria based on task type."""
        base = {
            "research": ["Identified key research questions", "Gathered evidence", "Formed conclusions"],
            "creative_writing": ["Developed unique concept", "Created compelling piece", "Achieved creative expression"],
            "analysis": ["Defined framework", "Gathered data", "Drew conclusions"],
            "problem_solving": ["Defined problem", "Generated solutions", "Selected approach"],
            "learning": ["Identified objectives", "Explored concepts", "Demonstrated understanding"],
        }
        return base.get(task_type, base["research"])

    def _classify_chain_type(self, topic: str, goal: str) -> str:
        """Classify chain type based on keywords."""
        combined = f"{topic} {goal}".lower()
        classifiers = [
            ("research", ["research", "investigate", "study", "explore", "discover"]),
            ("creative_writing", ["write", "create", "story", "poem", "creative", "compose"]),
            ("analysis", ["analyze", "compare", "evaluate", "assess", "examine"]),
            ("problem_solving", ["solve", "fix", "debug", "troubleshoot", "resolve"]),
            ("learning", ["learn", "understand", "master", "practice", "tutorial"]),
        ]
        for ctype, keys in classifiers:
            if any(k in combined for k in keys):
                return ctype
        return "research"

    def _update_milestone_progress(self, chain_data: Dict, response: str, insights: List[str]) -> None:
        """Update milestone progress based on response analysis."""
        try:
            # Pipeline of Actions (PoA) progress
            action_plan = chain_data.get("action_plan", {})
            if action_plan and action_plan.get("steps"):
                plan_steps = action_plan["steps"]
                current_step_idx = None
                for idx, step in enumerate(plan_steps):
                    if step.get("status") == "in_progress":
                        current_step_idx = idx
                        break
                    elif step.get("status") == "pending" and current_step_idx is None:
                        current_step_idx = idx
                        break
                if current_step_idx is not None and current_step_idx < len(plan_steps):
                    step = plan_steps[current_step_idx]
                    if step.get("status") != "completed":
                        check_words = step.get("description", "").lower().split()[:4]
                        resp_lower = response.lower()
                        if any(w in resp_lower for w in check_words):
                            step["status"] = "completed"
                            step["completed_at"] = time.time()
                            if current_step_idx + 1 < len(plan_steps):
                                plan_steps[current_step_idx + 1]["status"] = "in_progress"
                            logger.info(f"📌 PoA Step {current_step_idx+1}/{len(plan_steps)} completed: {step.get('description', '')[:50]}")

            # Legacy milestone progress
            milestones = chain_data.get("milestones", [])
            mc = chain_data.get("milestone_completion", {})
            resp_lower = response.lower()
            combined = resp_lower + " " + " ".join(insights).lower()
            for m in milestones:
                if m not in mc or not mc[m]:
                    key_words = m.lower().split()[:3]
                    if any(w in combined for w in key_words):
                        mc[m] = True
                        chain_data["metadata"]["milestones_completed"] = chain_data["metadata"].get("milestones_completed", 0) + 1
                        logger.info(f"🎯 Milestone completed: {m}")
            chain_data["milestone_completion"] = mc
        except Exception as e:
            logger.warning(f"Error updating milestone progress: {e}")

    def _estimate_steps_for_task(self, task_type: str) -> int:
        """Estimate the number of steps needed for a task type."""
        estimates = {
            "research": 12, "creative_writing": 10, "analysis": 15,
            "problem_solving": 8, "learning": 10, "code": 6,
            "trading": 12, "blockchain": 10,
        }
        return estimates.get(task_type, 10)

    def _generate_action_plan(self, task_type: str, topic: str, goal: str, total_steps: int) -> Dict[str, Any]:
        """Generate a structured Pipeline of Actions (PoA) action plan."""
        step_templates = {
            "research": [
                "Define research scope and key questions",
                "Search for background information and existing knowledge",
                "Gather primary sources and data points",
                "Analyze gathered information for patterns",
                "Synthesize findings into coherent conclusions",
                "Generate recommendations and next steps",
            ],
            "creative_writing": [
                "Brainstorm creative concept and themes",
                "Develop narrative structure and outline",
                "Draft initial creative content",
                "Refine and enhance creative elements",
                "Polish and finalize the piece",
            ],
            "analysis": [
                "Define analytical framework",
                "Gather data and evidence",
                "Perform initial analysis",
                "Identify key patterns and anomalies",
                "Draw conclusions from evidence",
                "Formulate actionable recommendations",
            ],
            "problem_solving": [
                "Define the problem precisely",
                "Analyze root causes",
                "Generate potential solutions",
                "Evaluate solutions",
                "Detail implementation plan",
            ],
        }
        templates = step_templates.get(task_type, step_templates["research"])
        steps = []
        for i, desc in enumerate(templates[:total_steps]):
            steps.append({
                "step_number": i + 1, "description": desc,
                "status": "in_progress" if i == 0 else "pending",
                "tools_suggested": [],
            })
        return {
            "task_type": task_type, "topic": topic, "goal": goal,
            "total_planned_steps": len(steps), "steps": steps,
            "created_at": time.time(),
        }

    # ===================================================================== #
    #  11. CONTENT FILTERING & PHASE HELPERS                                #
    # ===================================================================== #

    def _filter_response_content(self, response: str, chain_data: Dict) -> str:
        """Filter out tool call artifacts and clean response content."""
        if not response:
            return response
        lines = response.split("\n")
        filtered = []
        skip = False
        for line in lines:
            if line.strip().startswith("TOOL_CALL:") or line.strip().startswith('{"tool_name"'):
                skip = True
                continue
            if skip and line.strip() == "":
                skip = False
                continue
            if skip:
                continue
            if any(line.strip().startswith(p) for p in ["TOOL_RESULT:", "Tool Result:", "```tool_result"]):
                skip = True
                continue
            filtered.append(line)
        result = "\n".join(filtered).strip()
        if not result and response:
            result = response[:500]
        return result

    def _detect_concept_repetition(self, response: str, chain_data: Dict) -> float:
        """Detect how much of the response repeats previous concepts (0.0-1.0)."""
        try:
            if not chain_data.get("chain_sequence"):
                return 0.0
            prev = chain_data["chain_sequence"][-3:]
            prev_concepts = set()
            for s in prev:
                r = s.get("response", "")
                words = set(r.lower().split())
                stops = {"the", "a", "an", "is", "are", "was", "were", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "it", "this", "that"}
                prev_concepts.update(words - stops)
            new_words = set(response.lower().split())
            stops = {"the", "a", "an", "is", "are", "was", "were", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "it", "this", "that"}
            new_words -= stops
            if not new_words:
                return 0.0
            overlap = len(new_words & prev_concepts) / len(new_words)
            return overlap
        except Exception:
            return 0.0

    def _responses_are_too_similar(self, response: str, chain_data: Dict) -> bool:
        """Check if the new response is too similar to previous responses."""
        seq = chain_data.get("chain_sequence", [])
        if not seq:
            return False
        last_resp = seq[-1].get("response", "")
        if not last_resp or not response:
            return False
        last_words = set(last_resp.lower().split())
        new_words = set(response.lower().split())
        if not last_words or not new_words:
            return False
        overlap = len(last_words & new_words) / max(len(last_words | new_words), 1)
        return overlap > 0.85

    def _extract_key_concepts(self, response: str) -> List[str]:
        """Extract key concepts from a response."""
        words = response.lower().split()
        stops = {"the", "a", "an", "is", "are", "was", "were", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with", "it", "this", "that", "be", "have", "has", "had", "do", "does", "did", "will", "would", "could", "should", "may", "might", "can"}
        meaningful = [w for w in words if len(w) > 3 and w not in stops]
        from collections import Counter
        common = Counter(meaningful).most_common(10)
        return [w for w, _ in common]

    def _extract_key_themes_from_responses(self, responses: List[str]) -> List[str]:
        """Extract key themes from a list of responses."""
        if not responses:
            return []
        combined = " ".join(responses).lower()
        theme_kw = {
            "technology": ["technology", "system", "implementation", "architecture"],
            "research": ["research", "study", "analysis", "data"],
            "creative": ["creative", "design", "concept", "idea"],
            "practical": ["application", "solution", "deployment"],
            "theoretical": ["theory", "model", "framework"],
        }
        found = []
        for name, kws in theme_kw.items():
            if any(k in combined for k in kws):
                found.append(name)
        return found if found else ["general"]

    def _has_selected_concept(self, chain_data: Dict) -> bool:
        """Check if a concept has been selected in the chain."""
        for step in chain_data.get("chain_sequence", []):
            resp = step.get("response", "").lower()
            if any(p in resp for p in ["i'll focus on", "selected:", "chosen topic:", "i choose", "focusing on"]):
                return True
        return False

    def _has_sufficient_specifications(self, chain_data: Dict) -> bool:
        """Check if the chain has developed sufficient specifications."""
        spec_count = 0
        for step in chain_data.get("chain_sequence", []):
            resp = step.get("response", "").lower()
            if any(p in resp for p in ["specification", "requirement", "detail", "define", "criteria", "parameter"]):
                spec_count += 1
        return spec_count >= 2

    def _is_currently_implementable(self, chain_data: Dict) -> bool:
        """Check if the output phase work is currently implementable."""
        seq = chain_data.get("chain_sequence", [])
        if not seq:
            return False
        recent = seq[-3:] if len(seq) >= 3 else seq
        for step in recent:
            resp = step.get("response", "").lower()
            if any(p in resp for p in ["code:", "```", "implementation:", "def ", "class ", "function "]):
                return True
            tr = step.get("tool_results", {})
            if tr and isinstance(tr, dict) and tr.get("tool_calls_executed"):
                return True
        return False

    def _validate_and_fix_step_numbers(self, chain_data: Dict) -> bool:
        """Validate and fix sequential step numbers in chain data."""
        seq = chain_data.get("chain_sequence", [])
        fixed = False
        for i, step in enumerate(seq):
            expected = i + 1
            if step.get("step_number") != expected:
                step["step_number"] = expected
                fixed = True
        return fixed

    # ===================================================================== #
    #  12. EXPLORATION HISTORY, THOUGHTS, PRIORITY                          #
    # ===================================================================== #

    def query_exploration_history(self, query: str = "", limit: int = 20) -> Dict[str, Any]:
        """Query chain-of-thought exploration history (completed topics + search history)."""
        try:
            completed_topics = self._load_completed_cot_topics()
            recent_searches = self._load_recent_grokipedia_searches()
            active_chains = []
            if self._chains_dir.exists():
                for f in sorted(self._chains_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:5]:
                    try:
                        with open(f, "r") as fh:
                            d = json.load(fh)
                        active_chains.append({
                            "id": f.stem,
                            "topic": d.get("metadata", {}).get("topic", "Unknown"),
                            "progress": d.get("progress_level", 0),
                            "steps": len(d.get("chain_sequence", [])),
                            "status": d.get("status", "unknown"),
                        })
                    except Exception:
                        pass

            if query:
                query_lower = query.lower()
                completed_topics = [t for t in completed_topics if query_lower in t.get("topic", "").lower()]
                active_chains = [c for c in active_chains if query_lower in c.get("topic", "").lower()]

            completed_topics = completed_topics[-limit:]
            insights = self._generate_exploration_history_insights(completed_topics, active_chains)

            return {
                "completed_topics": completed_topics,
                "active_chains": active_chains,
                "recent_searches": recent_searches[-10:],
                "total_completed": len(self._load_completed_cot_topics()),
                "insights": insights,
            }
        except Exception as e:
            logger.error(f"Error querying exploration history: {e}")
            return {"completed_topics": [], "active_chains": [], "recent_searches": [], "total_completed": 0, "insights": {}}

    def _generate_exploration_history_insights(self, topics: List[Dict], active: List[Dict]) -> Dict[str, Any]:
        """Generate insights about exploration patterns and coverage gaps."""
        try:
            all_topics = [t.get("topic", "") for t in topics]
            domain_counts: Dict[str, int] = {}
            domains = {
                "technology": ["tech", "software", "AI", "computer", "digital", "system"],
                "science": ["research", "biology", "physics", "chemistry", "math"],
                "philosophy": ["philosophy", "ethics", "consciousness", "meaning", "existence"],
                "economics": ["economy", "market", "trade", "finance", "business"],
                "creativity": ["art", "music", "writing", "creative", "design"],
            }
            for t in all_topics:
                tl = t.lower()
                for domain, kws in domains.items():
                    if any(k in tl for k in kws):
                        domain_counts[domain] = domain_counts.get(domain, 0) + 1
            over_explored = [d for d, c in domain_counts.items() if c > 3]
            under_explored = [d for d in domains if d not in domain_counts]

            return {
                "domain_distribution": domain_counts,
                "over_explored_domains": over_explored,
                "under_explored_domains": under_explored,
                "total_topics_analyzed": len(topics),
                "active_chain_count": len(active),
                "recommendation": (
                    f"Consider exploring: {', '.join(under_explored[:3])}"
                    if under_explored else "Good coverage across domains"
                ),
            }
        except Exception as e:
            logger.warning(f"Error generating history insights: {e}")
            return {}

    def store_thoughts(self, thoughts: str, chain_id: str = "", context: str = "") -> Dict[str, Any]:
        """Store thoughts in episodic memory and node2040 brain."""
        try:
            thought_data = {
                "thought": thoughts,
                "chain_id": chain_id,
                "context": context,
                "timestamp": time.time(),
            }
            epis = getattr(self.brain, "episodic_memory", None)
            if epis:
                epis.append(thought_data)
                while len(epis) > getattr(self.brain, "max_episodic_memory", 1000):
                    epis.pop(0)

            node_brain = getattr(self.brain, "node2040_brain", None)
            if node_brain and isinstance(node_brain, dict):
                if "recent_thoughts" not in node_brain:
                    node_brain["recent_thoughts"] = []
                node_brain["recent_thoughts"].append({
                    "thought": thoughts[:500], "chain_id": chain_id,
                    "timestamp": time.time(),
                })
                node_brain["recent_thoughts"] = node_brain["recent_thoughts"][-50:]

            return {"stored": True, "thought_length": len(thoughts), "chain_id": chain_id}
        except Exception as e:
            logger.error(f"Error storing thoughts: {e}")
            return {"stored": False, "error": str(e)}

    def get_active_chain_priority(self) -> Optional[Dict]:
        """Get the highest-priority active chain (manual first, then autonomous)."""
        try:
            if not self._chains_dir.exists():
                return None
            manual_chains = []
            auto_chains = []
            for f in self._chains_dir.glob("*.json"):
                try:
                    with open(f, "r") as fh:
                        d = json.load(fh)
                    status = d.get("status", "")
                    if status not in ["active", "in_progress"]:
                        continue
                    chain_type = d.get("metadata", {}).get("chain_type", "autonomous")
                    entry = {
                        "id": f.stem,
                        "topic": d.get("metadata", {}).get("topic", ""),
                        "type": chain_type,
                        "progress": d.get("progress_level", 0),
                        "steps": len(d.get("chain_sequence", [])),
                        "created_at": d.get("metadata", {}).get("created_at", 0),
                    }
                    if chain_type == "manual":
                        manual_chains.append(entry)
                    else:
                        auto_chains.append(entry)
                except Exception:
                    pass
            if manual_chains:
                return sorted(manual_chains, key=lambda x: x.get("created_at", 0), reverse=True)[0]
            if auto_chains:
                return sorted(auto_chains, key=lambda x: x.get("progress", 0))[0]
            return None
        except Exception as e:
            logger.error(f"Error getting active chain priority: {e}")
            return None

    def _generate_specific_exploration_goal(self, topic: str, existing_topics: List[str]) -> str:
        """Generate a specific exploration goal, differentiating from existing topics."""
        try:
            call_ai = getattr(self.brain, "_call_ai_service", None)
            if call_ai and existing_topics:
                prompt = (
                    f"Generate a specific, unique exploration goal for the topic: '{topic}'\n\n"
                    f"AVOID overlap with these recent explorations:\n"
                    + "\n".join([f"- {t}" for t in existing_topics[-5:]])
                    + "\n\nRespond with ONLY the goal in one sentence."
                )
                try:
                    result = call_ai(prompt, timeout=60, include_tools=False)
                    if result and "AI_SERVICE_ERROR" not in result:
                        return result.strip()
                except Exception:
                    pass
            goal_templates = [
                f"Discover novel approaches and key principles underlying {topic}",
                f"Develop comprehensive understanding of {topic} and its implications",
                f"Explore practical applications and future potential of {topic}",
                f"Analyze the core mechanisms and relationships within {topic}",
                f"Investigate cutting-edge developments and emerging trends in {topic}",
            ]
            import random
            return random.choice(goal_templates)
        except Exception as e:
            logger.warning(f"Error generating exploration goal: {e}")
            return f"Explore and understand {topic}"

    def get_chain_context(self, chain_id: str, max_steps: int = 5) -> str:
        """Get formatted chain context for prompt inclusion."""
        try:
            chain_file = self._chains_dir / f"{chain_id}.json"
            if not chain_file.exists():
                return ""
            with open(chain_file, "r") as f:
                chain_data = json.load(f)
            topic = chain_data.get("metadata", {}).get("topic", "Unknown")
            goal = chain_data.get("metadata", {}).get("goal", "Unknown")
            progress = chain_data.get("progress_level", 0)
            phase = chain_data.get("current_phase", "exploration")

            parts = [
                f"ACTIVE CHAIN: {topic}",
                f"Goal: {goal}",
                f"Phase: {phase} | Progress: {progress*100:.0f}%",
            ]
            seq = chain_data.get("chain_sequence", [])
            recent = seq[-max_steps:] if len(seq) > max_steps else seq
            if recent:
                parts.append("RECENT STEPS:")
                for step in recent:
                    resp = step.get("response", "")
                    parts.append(f"  Step {step.get('step_number', '?')}: {resp[:200]}...")
            insights = chain_data.get("overall_insights", [])[-3:]
            if insights:
                parts.append("KEY INSIGHTS: " + "; ".join(insights))
            return "\n".join(parts)
        except Exception as e:
            logger.warning(f"Error getting chain context: {e}")
            return ""

    def _load_recent_grokipedia_searches(self) -> List[Dict]:
        """Load recent grokipedia searches with 24hr cleanup."""
        try:
            path = self.brain_path / "recent_grokipedia_searches.json"
            if not path.exists():
                return []
            with open(path, "r") as f:
                searches = json.load(f)
            cutoff = time.time() - 86400
            recent = [s for s in searches if s.get("timestamp", 0) > cutoff]
            if len(recent) < len(searches):
                with open(path, "w") as f:
                    json.dump(recent, f, indent=2)
            return recent
        except Exception:
            return []

    def _save_personality_brain(self) -> None:
        """Delegate personality brain saving to the brain system."""
        try:
            save_fn = getattr(self.brain, "_save_personality_brain", None)
            if save_fn:
                save_fn()
        except Exception as e:
            logger.warning(f"Error saving personality brain: {e}")
