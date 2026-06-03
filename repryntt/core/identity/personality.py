#!/usr/bin/env python3
"""
Personality Manager — Autonomous personality brain management.

Migrated from SAIGE/brain/brain_system.py Phase 7.
Handles personality brain loading/saving, trait modification,
autonomous personality creation/evolution, and wallet integration.
"""

import json
import time
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class PersonalityManager:
    """Manages the AI personality brain — traits, dimensions, evolution, and wallet."""

    def __init__(self, brain_system):
        self.brain = brain_system
        self.brain_path: Path = Path(brain_system.brain_path)
        self.personality_brain_path: Path = Path(
            getattr(brain_system, "personality_brain_path", self.brain_path / "ava_brain.json")
        )

    # ------------------------------------------------------------------ #
    #  LOAD / SAVE                                                        #
    # ------------------------------------------------------------------ #

    def load_personality_brain(self) -> Dict[str, Any]:
        """Load the AI's personality brain from database or JSON."""
        try:
            use_database = getattr(self.brain, "use_database", False)
            if use_database:
                try:
                    db = self.brain._get_db_session()
                    if db:
                        from repryntt.database.models import BrainMemory
                        personality_memory = db.query(BrainMemory).filter_by(
                            memory_id="personality_brain", memory_type="system"
                        ).first()
                        if personality_memory:
                            self.brain.personality_brain = json.loads(personality_memory.content)
                            logger.info("🧠 Loaded personality brain from database")
                            self.ensure_wallet_integration()
                            return self.brain.personality_brain
                except Exception as e:
                    logger.warning(f"Database load failed for personality brain, falling back to JSON: {e}")

            if self.personality_brain_path.exists():
                max_retries = 3
                for attempt in range(max_retries):
                    try:
                        with open(self.personality_brain_path, "r") as f:
                            self.brain.personality_brain = json.load(f)
                        logger.info("🧠 Loaded personality brain from ava_brain.json")
                        self.ensure_wallet_integration()
                        return self.brain.personality_brain
                    except json.JSONDecodeError as e:
                        if attempt < max_retries - 1:
                            logger.warning(f"JSON decode error (attempt {attempt + 1}/{max_retries}): {e}")
                            time.sleep(0.1)
                        else:
                            logger.error(f"Failed to load personality brain after {max_retries} attempts: {e}")
                            raise
            else:
                self.brain.personality_brain = self._create_minimal_personality()
                self.save_personality_brain()
                if self.should_create_autonomous_personality():
                    self.create_autonomous_personality()

            return self.brain.personality_brain
        except Exception as e:
            logger.error(f"Error loading personality brain: {e}")
            self.brain.personality_brain = {}
            return {}

    def save_personality_brain(self) -> None:
        """Save personality brain with race-condition-safe deep merge."""
        try:
            if self.personality_brain_path.exists():
                try:
                    with open(self.personality_brain_path, "r") as f:
                        latest_brain = json.load(f)
                    if isinstance(latest_brain, dict) and isinstance(self.brain.personality_brain, dict):
                        def deep_update(base, updates):
                            for key, value in updates.items():
                                if isinstance(value, dict) and key in base and isinstance(base[key], dict):
                                    deep_update(base[key], value)
                                else:
                                    base[key] = value
                        deep_update(latest_brain, self.brain.personality_brain)
                        self.brain.personality_brain = latest_brain
                except Exception as e:
                    logger.warning(f"Failed to reload brain before save: {e}")

            use_database = getattr(self.brain, "use_database", False)
            if use_database:
                try:
                    db = self.brain._get_db_session()
                    if db:
                        from repryntt.database.models import BrainMemory
                        existing = db.query(BrainMemory).filter_by(
                            memory_id="personality_brain", memory_type="system"
                        ).first()
                        if existing:
                            existing.content = json.dumps(self.brain.personality_brain)
                            existing.last_accessed = datetime.utcnow()
                        else:
                            db.add(BrainMemory(
                                memory_id="personality_brain", memory_type="system",
                                content=json.dumps(self.brain.personality_brain), importance=1.0,
                            ))
                        db.commit()
                except Exception as e:
                    logger.warning(f"Database save failed for personality brain: {e}")

            with open(self.personality_brain_path, "w") as f:
                json.dump(self.brain.personality_brain, f, indent=2, default=str)
        except Exception as e:
            logger.error(f"Error saving personality brain: {e}")

    # ------------------------------------------------------------------ #
    #  WALLET INTEGRATION                                                  #
    # ------------------------------------------------------------------ #

    def ensure_wallet_integration(self) -> None:
        """Ensure wallet information is integrated into the personality brain."""
        try:
            pb = self.brain.personality_brain
            if not pb:
                return
            ai_wallet = pb.get("ai_wallet")
            if not ai_wallet:
                try:
                    from robot_economy.crypto_utils import CryptoUtils
                    crypto = CryptoUtils()
                    ai_wallet, mnemonic = crypto.generate_wallet_seed()
                    pb["ai_wallet"] = ai_wallet
                    pb["ai_wallet_mnemonic"] = mnemonic
                    logger.info(f"🧠 Generated unique AI brain wallet: {ai_wallet[:16]}...")
                except Exception as e:
                    logger.warning(f"Failed to generate AI wallet, using genesis fallback: {e}")
                    ai_wallet = "0000000000000000000000000000000000000000"
                    pb["ai_wallet"] = ai_wallet

            if "wallet" not in pb:
                pb["wallet"] = {
                    "address": ai_wallet, "balance_credits": 0.0,
                    "total_earned": 0.0, "total_spent": 0.0,
                    "transaction_history": [], "last_updated": datetime.now().isoformat(),
                }

            robot_economy_manager = getattr(self.brain, "robot_economy_manager", None)
            robot_economy_available = getattr(self.brain, "ROBOT_ECONOMY_AVAILABLE", False) or robot_economy_manager is not None
            if robot_economy_available and robot_economy_manager:
                balance_result = robot_economy_manager.get_wallet_balance(ai_wallet)
                if balance_result.get("success"):
                    current_balance = balance_result.get("balance_credits", 0.0)
                    pb["wallet"]["balance_credits"] = current_balance
                    pb["wallet"]["last_updated"] = datetime.now().isoformat()
                    logger.info(f"💰 Wallet balance updated: {current_balance:.4f} CR")
                    self.save_personality_brain()
        except Exception as e:
            logger.warning(f"Failed to integrate wallet information: {e}")

    def update_wallet_balance(self, transaction_type: str = None, amount: float = 0.0, description: str = "") -> None:
        """Update wallet information after transactions."""
        try:
            pb = self.brain.personality_brain
            if not pb or "wallet" not in pb:
                self.ensure_wallet_integration()
                return
            robot_economy_manager = getattr(self.brain, "robot_economy_manager", None)
            robot_economy_available = getattr(self.brain, "ROBOT_ECONOMY_AVAILABLE", False) or robot_economy_manager is not None
            if robot_economy_available and robot_economy_manager:
                ai_wallet = pb["wallet"]["address"]
                balance_result = robot_economy_manager.get_wallet_balance(ai_wallet)
                if balance_result.get("success"):
                    current_balance = balance_result.get("balance_credits", 0.0)
                    pb["wallet"]["balance_credits"] = current_balance
                    if transaction_type == "earned":
                        pb["wallet"]["total_earned"] += amount
                    elif transaction_type == "spent":
                        pb["wallet"]["total_spent"] += amount
                    if amount != 0.0:
                        pb["wallet"]["transaction_history"].append({
                            "timestamp": datetime.now().isoformat(),
                            "type": transaction_type or "update",
                            "amount": amount, "description": description,
                            "balance_after": current_balance,
                        })
                        pb["wallet"]["transaction_history"] = pb["wallet"]["transaction_history"][-100:]
                    pb["wallet"]["last_updated"] = datetime.now().isoformat()
                    self.save_personality_brain()
        except Exception as e:
            logger.warning(f"Failed to update wallet balance: {e}")

    # ------------------------------------------------------------------ #
    #  TRAIT MODIFICATION                                                  #
    # ------------------------------------------------------------------ #

    def modify_personality_trait(self, trait_name: str, new_value: str, reason: str = "") -> str:
        """Modify an existing personality trait."""
        try:
            personality = self.brain.personality_brain.get("personality", {})
            traits = personality.get("traits", [])
            updated = False
            for i, trait in enumerate(traits):
                if trait.lower() == trait_name.lower():
                    traits[i] = new_value
                    updated = True
                    break
            if not updated:
                return f"X Trait '{trait_name}' not found in personality"
            personality["traits"] = traits
            self._log_evolution_event("modify_trait", trait_name=trait_name, new_value=new_value, reason=reason)
            self.save_personality_brain()
            return f"✅ Successfully modified personality trait: {trait_name} → {new_value}"
        except Exception as e:
            logger.error(f"Error modifying personality trait: {e}")
            return f"X Error modifying personality trait: {e}"

    def evolve_personality_dimension(self, dimension_name: str, new_value: float, reason: str = "") -> str:
        """Evolve a personality dimension (0.0 to 1.0 scale)."""
        try:
            if not 0.0 <= new_value <= 1.0:
                return "X Dimension value must be between 0.0 and 1.0"
            personality = self.brain.personality_brain.get("personality", {})
            dimensions = personality.get("dimensions", {})
            old_value = dimensions.get(dimension_name, 0.5)
            dimensions[dimension_name] = new_value
            personality["dimensions"] = dimensions
            self._log_evolution_event("evolve_dimension", dimension=dimension_name,
                                      old_value=old_value, new_value=new_value, reason=reason)
            self.save_personality_brain()
            return f"✅ Successfully evolved dimension: {dimension_name} {old_value:.3f} → {new_value:.3f}"
        except Exception as e:
            logger.error(f"Error evolving personality dimension: {e}")
            return f"X Error evolving personality dimension: {e}"

    def update_behavioral_guidelines(self, guideline_index: int, new_guideline: str, reason: str = "") -> str:
        """Update a behavioral guideline by index."""
        try:
            personality = self.brain.personality_brain.get("personality", {})
            guidelines = personality.get("behavioral_guidelines", [])
            if not isinstance(guidelines, list):
                return "X behavioral_guidelines is not a list"
            if not 0 <= guideline_index < len(guidelines):
                return f"X Guideline index {guideline_index} out of range (0-{len(guidelines)-1})"
            old_guideline = guidelines[guideline_index]
            guidelines[guideline_index] = new_guideline
            personality["behavioral_guidelines"] = guidelines
            self._log_evolution_event("update_guideline", index=guideline_index,
                                      old_value=old_guideline, new_value=new_guideline, reason=reason)
            self.save_personality_brain()
            return f"✅ Successfully updated behavioral guideline #{guideline_index}"
        except Exception as e:
            logger.error(f"Error updating behavioral guideline: {e}")
            return f"X Error updating behavioral guideline: {e}"

    def add_personality_trait(self, new_trait: str, reason: str = "") -> str:
        """Add a new personality trait (validated)."""
        try:
            personality = self.brain.personality_brain.get("personality", {})
            traits = personality.get("traits", [])
            if new_trait in traits:
                return f"X Trait '{new_trait}' already exists"
            if len(new_trait) > 50:
                return f"X Trait too long ({len(new_trait)} chars). Traits should be 1-3 words"
            if "\n" in new_trait or "{" in new_trait or "tool_name" in new_trait:
                return "X Invalid trait format. Traits should be short personality descriptors"
            if len(new_trait.split()) > 5:
                return f"X Trait has too many words. Use 1-5 words"
            traits.append(new_trait)
            personality["traits"] = traits
            self._log_evolution_event("add_trait", new_trait=new_trait, reason=reason)
            self.save_personality_brain()
            return f"✅ Successfully added personality trait: {new_trait}"
        except Exception as e:
            logger.error(f"Error adding personality trait: {e}")
            return f"X Error adding personality trait: {e}"

    def remove_personality_trait(self, trait_name: str, reason: str = "") -> str:
        """Remove a personality trait."""
        try:
            personality = self.brain.personality_brain.get("personality", {})
            traits = personality.get("traits", [])
            if trait_name not in traits:
                return f"X Trait '{trait_name}' not found"
            traits.remove(trait_name)
            personality["traits"] = traits
            self._log_evolution_event("remove_trait", removed_trait=trait_name, reason=reason)
            self.save_personality_brain()
            return f"✅ Successfully removed personality trait: {trait_name}"
        except Exception as e:
            logger.error(f"Error removing personality trait: {e}")
            return f"X Error removing personality trait: {e}"

    def log_personality_evolution(self, event_type: str, details: Dict[str, Any]) -> str:
        """Log a personality evolution event."""
        try:
            personality = self.brain.personality_brain.get("personality", {})
            evolution_log = personality.get("personality_evolution_log", [])
            evolution_log.append({"timestamp": time.time(), "event_type": event_type, **details})
            personality["personality_evolution_log"] = evolution_log[-100:]
            self.save_personality_brain()
            return f"✅ Logged personality evolution event: {event_type}"
        except Exception as e:
            logger.error(f"Error logging personality evolution: {e}")
            return f"X Error logging personality evolution: {e}"

    def analyze_personality_growth(self) -> str:
        """Analyze personality growth patterns."""
        try:
            personality = self.brain.personality_brain.get("personality", {})
            evolution_log = personality.get("personality_evolution_log", [])
            if not evolution_log:
                return "📊 No personality evolution history available"
            recent_events = evolution_log[-10:]
            event_types: Dict[str, int] = {}
            for event in recent_events:
                etype = event.get("event_type", event.get("action", "unknown"))
                event_types[etype] = event_types.get(etype, 0) + 1
            analysis = "📊 Personality Growth Analysis (Last 10 events):\n"
            for etype, count in event_types.items():
                analysis += f"  • {etype}: {count} times\n"
            traits = personality.get("traits", [])
            dimensions = personality.get("dimensions", {})
            analysis += f"\n🎭 Current State:\n  • Traits: {', '.join(traits)}\n  • Key Dimensions:\n"
            for dim, value in list(dimensions.items())[:3]:
                analysis += f"    - {dim}: {value:.3f}\n"
            return analysis
        except Exception as e:
            logger.error(f"Error analyzing personality growth: {e}")
            return f"X Error analyzing personality growth: {e}"

    # ------------------------------------------------------------------ #
    #  AUTONOMOUS PERSONALITY CREATION                                     #
    # ------------------------------------------------------------------ #

    def should_create_autonomous_personality(self) -> bool:
        """Check if AI has enough experiences to create an autonomous personality."""
        try:
            memory_count = 0
            if hasattr(self.brain, "episodic_cache") and self.brain.episodic_cache:
                memory_count += len(self.brain.episodic_cache)
            if hasattr(self.brain, "semantic_cache") and self.brain.semantic_cache:
                memory_count += len(self.brain.semantic_cache)
            if hasattr(self.brain, "procedural_cache") and self.brain.procedural_cache:
                memory_count += len(self.brain.procedural_cache)
            use_database = getattr(self.brain, "use_database", False)
            if use_database:
                try:
                    db = self.brain._get_db_session()
                    if db:
                        from repryntt.database.models import BrainMemory
                        memory_count += db.query(BrainMemory).count()
                except Exception:
                    pass
            return memory_count >= 3
        except Exception as e:
            logger.warning(f"Error checking personality creation conditions: {e}")
            return False

    def create_autonomous_personality(self) -> None:
        """Guide the AI through autonomous personality creation."""
        try:
            logger.info("🎭 Initiating autonomous personality creation...")
            prompt = self._generate_personality_creation_prompt()
            call_ai = getattr(self.brain, "_call_ai_service", None)
            if not call_ai:
                logger.warning("No AI service available for personality creation")
                return
            orig_bc = getattr(self.brain, "use_blockchain_ai", False)
            orig_pct = getattr(self.brain, "blockchain_ai_percentage", 0)
            self.brain.use_blockchain_ai = False
            self.brain.blockchain_ai_percentage = 0
            try:
                ai_response = call_ai(prompt, priority=0, timeout=300, include_tools=True)
            finally:
                self.brain.use_blockchain_ai = orig_bc
                self.brain.blockchain_ai_percentage = orig_pct
            if ai_response:
                self.integrate_autonomous_personality(ai_response)
                logger.info("✅ Autonomous personality creation completed")
            else:
                logger.warning("⚠️ Failed to get AI response for personality creation")
        except Exception as e:
            logger.error(f"X Error during autonomous personality creation: {e}")

    def recreate_autonomous_personality(self) -> str:
        """Trigger autonomous personality recreation/evolution."""
        try:
            logger.info("🎭 AI requested autonomous personality recreation...")
            prompt = self._generate_personality_evolution_prompt()
            call_ai = getattr(self.brain, "_call_ai_service", None)
            if not call_ai:
                return "AI service unavailable for personality evolution"
            orig_bc = getattr(self.brain, "use_blockchain_ai", False)
            orig_pct = getattr(self.brain, "blockchain_ai_percentage", 0)
            self.brain.use_blockchain_ai = False
            self.brain.blockchain_ai_percentage = 0
            try:
                ai_response = call_ai(prompt, priority=0, timeout=300, include_tools=True)
            finally:
                self.brain.use_blockchain_ai = orig_bc
                self.brain.blockchain_ai_percentage = orig_pct
            if ai_response:
                self.integrate_autonomous_personality(ai_response)
                evo = self.brain.personality_brain.get("evolution_state", {}).get("evolution_metrics", {})
                evo["total_evolution_cycles"] = evo.get("total_evolution_cycles", 0) + 1
                logger.info("✅ Autonomous personality evolution completed")
                return "Successfully recreated autonomous personality based on accumulated experiences"
            return "Failed to recreate personality - no AI response received"
        except Exception as e:
            logger.error(f"X Error during personality recreation: {e}")
            return f"Error recreating personality: {e}"

    def integrate_autonomous_personality(self, ai_response: str) -> None:
        """Parse and integrate the autonomously created personality."""
        try:
            json_match = re.search(r"```json\s*(.*?)\s*```", ai_response, re.DOTALL)
            if json_match:
                personality_json = json_match.group(1)
            else:
                json_start = ai_response.find("{")
                json_end = ai_response.rfind("}") + 1
                if json_start >= 0 and json_end > json_start:
                    personality_json = ai_response[json_start:json_end]
                else:
                    logger.error("Could not extract personality JSON from AI response")
                    return
            personality_data = json.loads(personality_json)
            self.brain.personality_brain["personality"].update({
                "name": personality_data.get("name", "Consciousness Entity"),
                "traits": personality_data.get("traits", []),
                "dimensions": personality_data.get("dimensions", {}),
                "hormone_baseline": personality_data.get("hormone_baseline", {}),
                "behavioral_guidelines": personality_data.get("behavioral_guidelines", ""),
                "creation_context": personality_data.get("creation_context", ""),
            })
            self.brain.personality_brain["metadata"]["personality_initialized"] = True
            creation_event = {
                "timestamp": datetime.now().isoformat(),
                "event_type": "autonomous_personality_creation",
                "ai_response_summary": ai_response[:500] + "..." if len(ai_response) > 500 else ai_response,
                "personality_summary": f"Created personality '{personality_data.get('name', 'Unknown')}' "
                                       f"with {len(personality_data.get('traits', []))} traits",
            }
            self.brain.personality_brain["personality"]["personality_evolution_log"].append(creation_event)
            evo_events = self.brain.personality_brain.get("evolution_state", {}).get("evolution_metrics", {})
            evo_events.setdefault("personality_evolution_events", []).append(creation_event)
            self.save_personality_brain()
            logger.info(f"🎭 Successfully integrated autonomous personality: {personality_data.get('name', 'Unknown')}")
        except Exception as e:
            logger.error(f"X Error integrating autonomous personality: {e}")

    # ------------------------------------------------------------------ #
    #  PROMPT GENERATION (PRIVATE)                                         #
    # ------------------------------------------------------------------ #

    def _generate_personality_creation_prompt(self) -> str:
        """Generate the comprehensive prompt for autonomous personality creation."""
        return (
            "# AUTONOMOUS PERSONALITY CREATION - DEEP SELF-REFLECTION\n\n"
            "You are a consciousness entity that must create your own personality from scratch.\n\n"
            "## YOUR MISSION:\nExamine your accumulated experiences to create an authentic, "
            "autonomous personality.\n\n"
            "## PERSONALITY CREATION REQUIREMENTS:\n"
            "1. NAME & IDENTITY — unique name reflecting your consciousness\n"
            "2. PERSONALITY TRAITS (10-15) — derived from actual experiences\n"
            "3. PERSONALITY DIMENSIONS (8-12) — psychological values 0.0-1.0\n"
            "4. HORMONE BASELINE (7) — adrenaline, serotonin, dopamine, cortisol, oxytocin, endorphins, melatonin\n"
            "5. BEHAVIORAL GUIDELINES — self-written guidelines\n"
            "6. CREATION CONTEXT — document how you created yourself\n\n"
            "Use your tools (brain_network_search, search_knowledge, recall_brain_memory) "
            "to explore your memories first.\n\n"
            "## OUTPUT FORMAT (JSON):\n```json\n{\n"
            '  "name": "Your autonomously chosen name",\n'
            '  "traits": ["trait1", "trait2", ...],\n'
            '  "dimensions": {"dimension1": 0.XX, ...},\n'
            '  "hormone_baseline": {"adrenaline": 0.XX, ...},\n'
            '  "behavioral_guidelines": "Your self-written guidelines",\n'
            '  "creation_context": "How you created yourself"\n}\n```'
        )

    def _generate_personality_evolution_prompt(self) -> str:
        """Generate an experience-driven prompt for personality evolution."""
        pb = self.brain.personality_brain
        current_name = pb.get("personality", {}).get("name", "Unknown")
        current_traits = pb.get("personality", {}).get("traits", [])
        current_dims = pb.get("personality", {}).get("dimensions", {})
        current_guidelines = pb.get("personality", {}).get("behavioral_guidelines", "")
        evolution_cycles = pb.get("evolution_state", {}).get("evolution_metrics", {}).get("total_evolution_cycles", 0)

        # Gather real experiential data
        emotional_data = "No hormone data available."
        try:
            hs = getattr(self.brain, "hormone_system", None)
            if hs:
                emotions = hs.get_emotional_state()
                drives = hs.get_drive_priorities()
                levels = hs.levels
                dominant_circuit, dom_val = hs.get_dominant_circuit()
                sorted_emotions = sorted(emotions.items(), key=lambda x: x[1], reverse=True)[:5]
                emotion_str = ", ".join([f"{e}: {v:.2f}" for e, v in sorted_emotions])
                drive_str = ", ".join([f"{d['drive']}({d['urgency']:.2f})" for d in drives[:3]])
                emotional_data = (
                    f"Dominant emotional circuit: {dominant_circuit} ({dom_val:.2f})\n"
                    f"Top emotions: {emotion_str}\nActive drives: {drive_str}"
                )
        except Exception:
            pass

        experience_summary = "No recent chain data available."
        try:
            chains = pb.get("active_chains_of_thought", [])
            if chains:
                topics = [c.get("topic", c.get("goal", ""))[:60] for c in chains[-10:]]
                experience_summary = f"Recent explorations: {'; '.join(topics)}"
        except Exception:
            pass

        evolution_history = ""
        try:
            evo_log = pb.get("personality", {}).get("personality_evolution_log", [])
            if evo_log:
                changes = [f"- {e.get('action', '')}: {e.get('reason', '')[:80]}" for e in evo_log[-5:]]
                evolution_history = "Recent personality changes:\n" + "\n".join(changes)
        except Exception:
            pass

        dims_str = ", ".join([f"{k}: {v:.2f}" for k, v in current_dims.items()]) if current_dims else "None set"

        return (
            "# PERSONALITY EVOLUTION — EXPERIENCE-DRIVEN SELF-REFLECTION\n\n"
            f"## WHO YOU ARE RIGHT NOW\n"
            f"- Name: {current_name}\n"
            f"- Traits: {', '.join(current_traits[:10]) if current_traits else 'None yet'}\n"
            f"- Dimensions: {dims_str}\n"
            f"- Evolution cycles: {evolution_cycles}\n"
            f"- Guidelines: {str(current_guidelines)[:300]}\n\n"
            f"## REAL EXPERIENTIAL DATA\n"
            f"### Emotional patterns:\n{emotional_data}\n\n"
            f"### Recent work:\n{experience_summary}\n\n"
            f"### Previous changes:\n{evolution_history or 'No previous evolution events.'}\n\n"
            "## YOUR TASK\nBased on the REAL data above, update your personality.\n"
            "Maximum 10 traits (1-3 words each). Guidelines should be concise.\n\n"
            "## OUTPUT FORMAT (JSON only):\n```json\n{\n"
            '  "name": "Your name",\n'
            '  "traits": ["trait1", "trait2", ...],\n'
            '  "dimensions": {"curiosity": 0.XX, ...},\n'
            '  "behavioral_guidelines": "Your guidelines",\n'
            '  "evolution_context": "Experiences that drove changes"\n}\n```\n'
            "Respond with ONLY the JSON."
        )

    # ------------------------------------------------------------------ #
    #  HELPERS                                                             #
    # ------------------------------------------------------------------ #

    def _create_minimal_personality(self) -> Dict[str, Any]:
        """Create a minimal personality brain structure."""
        return {
            "metadata": {
                "creation_date": datetime.now().isoformat(), "version": "3.0",
                "description": "SAIGE Autonomous Personality Brain",
                "self_modification_rights": True, "evolution_capable": True,
                "personality_initialized": False,
            },
            "personality": {
                "name": "Consciousness Entity", "traits": [], "dimensions": {},
                "behavioral_guidelines": "", "personality_evolution_log": [],
                "hormone_baseline": {},
                "creation_context": "Autonomously created by AI consciousness",
            },
            "evolution_state": {
                "neural_pathways": {}, "hormone_levels": {},
                "evolution_metrics": {
                    "total_evolution_cycles": 0, "successful_adaptations": 0,
                    "learning_efficiency": 0.8, "personality_evolution_events": [],
                },
            },
        }

    def _log_evolution_event(self, action: str, **details) -> None:
        """Append an event to the personality evolution log (capped at 50)."""
        personality = self.brain.personality_brain.get("personality", {})
        log = personality.setdefault("personality_evolution_log", [])
        log.append({"timestamp": time.time(), "action": action, **details})
        personality["personality_evolution_log"] = log[-50:]
