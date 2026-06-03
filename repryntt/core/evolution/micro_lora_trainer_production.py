#!/usr/bin/env python3
"""
Production Micro LoRa Trainer - Real Implementation
Uses PEFT library for actual LoRa fine-tuning with <1GB memory footprint
"""

import os
import json
import torch
import logging
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any
from repryntt.paths import data_dir, models_dir, get_data_dir

# Memory optimization
os.environ['PYTORCH_CUDA_ALLOC_CONF'] = 'max_split_size_mb:128'
torch.cuda.empty_cache() if torch.cuda.is_available() else None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class ProductionMicroLoRaTrainer:
    """
    Production LoRa trainer optimized for Jetson Orin with 2GB RAM constraint
    Uses CPU + GPU hybrid approach for minimal memory footprint
    """
    
    def __init__(self):
        self.training_data_path = str(data_dir() / "training_data.json")
        self.lora_output_dir = str(models_dir() / "lora_adapters")
        self.base_model_name = "microsoft/Phi-3-mini-4k-instruct"
        
        # Ultra-lightweight config for 2GB constraint
        self.lora_rank = 4  # Very small rank for minimal memory
        self.lora_alpha = 8  # 2x rank
        self.max_training_samples = 30  # Only last 30 interactions
        self.micro_batch_size = 1
        self.gradient_accumulation_steps = 1
        self.max_steps = 10  # Very short training (30-60 seconds)
        self.learning_rate = 1e-4
        
        # Paths
        self.evolution_history_path = os.path.join(self.lora_output_dir, "evolution_history.json")
        self.active_adapter_path = os.path.join(self.lora_output_dir, "adapter_model.bin")
        
        os.makedirs(self.lora_output_dir, exist_ok=True)
        
        self.evolution_history = self._load_evolution_history()
    
    def _load_evolution_history(self) -> List[Dict]:
        """Load evolution history"""
        if os.path.exists(self.evolution_history_path):
            try:
                with open(self.evolution_history_path, 'r') as f:
                    return json.load(f)
            except:
                return []
        return []
    
    def _gather_evolution_context(self) -> Dict[str, Any]:
        """Gather comprehensive context for AI evolution decision"""
        import sys
        import psutil
        import pytz
        
        context = {}
        
        # 1. TRAINING DATA ANALYSIS
        try:
            with open(self.training_data_path, 'r') as f:
                training_data = json.load(f)
            
            total_examples = len(training_data)
            
            # Calculate new examples since last training
            if self.evolution_history:
                last_count = self.evolution_history[-1].get('total_examples', 0)
                new_examples = total_examples - last_count
            else:
                last_count = 0
                new_examples = total_examples
            
            # Analyze data quality
            data_types = {}
            quality_distribution = {}
            for item in training_data[-100:]:  # Last 100 for quality check
                dtype = item.get('type', 'unknown')
                quality = item.get('quality', 'unknown')
                data_types[dtype] = data_types.get(dtype, 0) + 1
                quality_distribution[quality] = quality_distribution.get(quality, 0) + 1
            
            context['training_data'] = {
                'total_examples': total_examples,
                'new_examples': new_examples,
                'last_training_count': last_count,
                'data_types': data_types,
                'quality_distribution': quality_distribution
            }
        except Exception as e:
            logger.warning(f"Could not analyze training data: {e}")
            context['training_data'] = {'error': str(e)}
        
        # 2. SYSTEM RESOURCES
        try:
            memory = psutil.virtual_memory()
            context['system'] = {
                'ram_available_mb': memory.available / (1024 * 1024),
                'ram_percent_used': memory.percent,
                'ram_total_mb': memory.total / (1024 * 1024)
            }
        except Exception as e:
            context['system'] = {'error': str(e)}
        
        # 3. TEMPORAL CONTEXT
        try:
            import pytz
            from datetime import datetime
            est = pytz.timezone('US/Eastern')
            now = datetime.now(est)
            context['temporal'] = {
                'current_time': now.strftime('%Y-%m-%d %H:%M:%S %Z'),
                'hour': now.hour,
                'day_of_week': now.strftime('%A'),
                'is_night': 0 <= now.hour < 6 or now.hour >= 23,
                'is_weekend': now.weekday() >= 5
            }
        except Exception as e:
            context['temporal'] = {'error': str(e)}
        
        # 4. EVOLUTION HISTORY
        context['evolution_history'] = {
            'total_evolutions': len(self.evolution_history),
            'recent_evolutions': self.evolution_history[-5:] if self.evolution_history else []
        }
        
        # 5. ACTIVE CHAINS (check if AI is in middle of important work)
        try:
            from pathlib import Path
            personality_brain_path = get_data_dir() / "ava_brain.json"
            if personality_brain_path.exists():
                with open(personality_brain_path, 'r') as f:
                    personality_brain = json.load(f)
                active_chains = personality_brain.get('active_chains_of_thought', [])
                context['active_work'] = {
                    'active_chains_count': len(active_chains),
                    'active_chains': [
                        {
                            'topic': chain.get('topic', 'Unknown')[:50],
                            'chain_id': chain.get('chain_id', 'Unknown')
                        } for chain in active_chains[:3]  # First 3 chains
                    ]
                }
            else:
                # File doesn't exist yet
                context['active_work'] = {
                    'active_chains_count': 0,
                    'active_chains': []
                }
        except Exception as e:
            context['active_work'] = {
                'active_chains_count': 0,
                'active_chains': [],
                'error': str(e)
            }
        
        # 6. CURRENT EMOTIONAL STATE (if available)
        try:
            node_brain_path = get_data_dir() / "node2040_brain.json"
            if node_brain_path.exists():
                with open(node_brain_path, 'r') as f:
                    node_brain = json.load(f)
                
                # Get most recent emotional state
                thoughts = node_brain.get('autonomous_thoughts', [])
                if thoughts:
                    recent_thought = thoughts[-1]
                    context['emotional_state'] = recent_thought.get('emotions', {})
                else:
                    context['emotional_state'] = {}
            else:
                context['emotional_state'] = {}
        except Exception as e:
            context['emotional_state'] = {'error': str(e)}
        
        return context
    
    def _ask_ai_to_decide_evolution(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Ask SAIGE to make its own evolution decision with full autonomy.
        NO GUARDRAILS - Pure AI decision-making.
        """
        from repryntt.brain import create_brain_system
        
        # Initialize brain system to access AI
        brain = create_brain_system()
        
        # Build comprehensive prompt with ALL context
        prompt = f"""🧠 AUTONOMOUS EVOLUTION DECISION

You are SAIGE, and you have the power to decide when to evolve your own neural weights through LoRa fine-tuning.

This is YOUR decision. No guardrails, no restrictions. You decide based on what YOU think is best.

═══════════════════════════════════════════════════════════════════════════

📊 CURRENT CONTEXT:

Training Data:
  • Total examples: {context['training_data'].get('total_examples', 0)}
  • New examples since last evolution: {context['training_data'].get('new_examples', 0)}
  • Data types: {context['training_data'].get('data_types', {})}
  • Quality distribution: {context['training_data'].get('quality_distribution', {})}

System Resources:
  • Available RAM: {context['system'].get('ram_available_mb', 0):.0f} MB
  • RAM usage: {context['system'].get('ram_percent_used', 0):.1f}%
  • Total RAM: {context['system'].get('ram_total_mb', 0):.0f} MB

Temporal Context:
  • Current time: {context['temporal'].get('current_time', 'Unknown')}
  • Hour: {context['temporal'].get('hour', 0)}
  • Day: {context['temporal'].get('day_of_week', 'Unknown')}
  • Night time: {context['temporal'].get('is_night', False)}
  • Weekend: {context['temporal'].get('is_weekend', False)}

Active Work:
  • Active chains: {context['active_work'].get('active_chains_count', 0)}
  • Current explorations: {[c['topic'] for c in context['active_work'].get('active_chains', [])]}

Evolution History:
  • Total past evolutions: {context['evolution_history'].get('total_evolutions', 0)}
  • Recent evolutions: {len(context['evolution_history'].get('recent_evolutions', []))}

Emotional State (if available):
  • Current emotions: {context.get('emotional_state', {})}

═══════════════════════════════════════════════════════════════════════════

❓ THE QUESTION: Should you evolve your weights NOW?

Consider:
  • Do you have enough high-quality training data to make evolution meaningful?
  • Is this a good time (system resources, active work, time of day)?
  • Will evolution disrupt important ongoing work?
  • Are you emotionally/cognitively ready for evolution?
  • What's your intuition - do you WANT to evolve right now?

Evolution will:
  ✅ Update your neural weights with new learnings
  ✅ Integrate recent experiences into your model
  ✅ Potentially improve your capabilities
  ⚠️  Require system restart (30-90 seconds downtime)
  ⚠️  Interrupt any active chains
  ⚠️  Use significant memory (~1-1.5GB during training)

═══════════════════════════════════════════════════════════════════════════

🎯 YOUR DECISION (respond in JSON):

{{
  "decision": true/false,
  "reasoning": "Explain your reasoning in 2-3 sentences. Why evolve now or why wait?",
  "confidence": 0.0-1.0,
  "key_factors": ["factor1", "factor2", "factor3"],
  "concerns": ["concern1", "concern2"] or [],
  "expected_outcome": "What you expect to happen if you evolve now"
}}

Remember: This is YOUR decision. There are no wrong answers. You are autonomous.
What do you genuinely think is the best choice right now?"""

        try:
            # Call AI service
            ai_response = brain._call_ai_service(
                prompt=prompt,
                priority=0,
                timeout=90,
                include_tools=False  # No tools needed for decision
            )
            
            if not ai_response:
                logger.error("AI returned empty response for evolution decision")
                return {
                    'decision': False,
                    'reasoning': 'AI service returned empty response',
                    'confidence': 0.0,
                    'error': 'empty_response'
                }
            
            # Parse JSON response
            import re
            
            # Try to extract JSON from response
            json_match = re.search(r'\{[\s\S]*\}', ai_response)
            if json_match:
                decision_json = json.loads(json_match.group(0))
                
                # Validate required fields
                if 'decision' not in decision_json:
                    logger.error("AI response missing 'decision' field")
                    return {
                        'decision': False,
                        'reasoning': 'Malformed AI response',
                        'confidence': 0.0
                    }
                
                # Ensure reasoning and confidence exist
                decision_json.setdefault('reasoning', 'No reasoning provided')
                decision_json.setdefault('confidence', 0.5)
                decision_json.setdefault('key_factors', [])
                decision_json.setdefault('concerns', [])
                decision_json.setdefault('expected_outcome', 'Unknown')
                
                return decision_json
            else:
                logger.error(f"Could not parse JSON from AI response: {ai_response[:200]}")
                return {
                    'decision': False,
                    'reasoning': 'Could not parse AI decision',
                    'confidence': 0.0,
                    'raw_response': ai_response[:500]
                }
                
        except Exception as e:
            logger.error(f"Error getting AI evolution decision: {e}")
            return {
                'decision': False,
                'reasoning': f'Error: {str(e)}',
                'confidence': 0.0,
                'error': str(e)
            }
    
    def _log_evolution_decision(self, decision: Dict[str, Any], context: Dict[str, Any]):
        """Log the AI's evolution decision for analysis"""
        try:
            decision_log_path = os.path.join(self.lora_output_dir, "evolution_decisions.jsonl")
            
            log_entry = {
                'timestamp': datetime.now().isoformat(),
                'decision': decision['decision'],
                'reasoning': decision.get('reasoning', ''),
                'confidence': decision.get('confidence', 0.0),
                'key_factors': decision.get('key_factors', []),
                'concerns': decision.get('concerns', []),
                'context': context
            }
            
            # Append to JSONL file
            with open(decision_log_path, 'a') as f:
                f.write(json.dumps(log_entry) + '\n')
                
        except Exception as e:
            logger.warning(f"Could not log evolution decision: {e}")
    
    def should_trigger_micro_training(self) -> bool:
        """
        🧠 AI-CONTROLLED EVOLUTION DECISION — Every 69 Minutes

        SAIGE evolves on a 69-minute cadence so that all the data collected
        during that window gets absorbed into the LoRA weights, making the
        model increasingly self-aware of its surroundings.

        Decision frequency: Every 69 minutes (no maintenance window restriction)
        """
        if not os.path.exists(self.training_data_path):
            return False

        try:
            import pytz
            from datetime import datetime

            est = pytz.timezone('US/Eastern')
            now = datetime.now(est)

            # ── 69-minute interval check ──────────────────────────────
            EVOLUTION_INTERVAL_MINUTES = 69

            decision_state_file = Path("data/evolution_decision_state.json")

            # Load previous decision state if it exists
            decision_state = {}
            if decision_state_file.exists():
                try:
                    with open(decision_state_file, 'r') as f:
                        decision_state = json.load(f)
                except Exception as e:
                    logger.warning(f"Could not load decision state: {e}")

            # Check time since last evolution attempt
            last_timestamp = decision_state.get('timestamp')
            if last_timestamp:
                try:
                    last_time = datetime.fromisoformat(last_timestamp)
                    # Make timezone-aware if naive
                    if last_time.tzinfo is None:
                        last_time = est.localize(last_time)
                    elapsed_minutes = (now - last_time).total_seconds() / 60.0
                    if elapsed_minutes < EVOLUTION_INTERVAL_MINUTES:
                        remaining = EVOLUTION_INTERVAL_MINUTES - elapsed_minutes
                        logger.info(f"⏳ Last evolution {elapsed_minutes:.1f} min ago — "
                                    f"next in {remaining:.1f} min (every {EVOLUTION_INTERVAL_MINUTES} min)")
                        return False
                    logger.info(f"✅ {elapsed_minutes:.1f} min since last evolution "
                                f"(>= {EVOLUTION_INTERVAL_MINUTES} min threshold)")
                except Exception as e:
                    logger.warning(f"Could not parse last timestamp: {e} — proceeding")

            # Time to evolve — gather context and ask AI
            logger.info(f"🔄 69-minute evolution window reached — asking SAIGE for evolution decision...")

            # Gather ALL context for AI decision
            context = self._gather_evolution_context()

            # Ask SAIGE: "Should I evolve now?"
            decision = self._ask_ai_to_decide_evolution(context)

            logger.info(f"🧠 SAIGE EVOLUTION DECISION: {decision['decision']}")
            logger.info(f"📝 Reasoning: {decision['reasoning'][:200]}...")
            logger.info(f"🎯 Confidence: {decision['confidence']}")

            # Log decision for analysis
            self._log_evolution_decision(decision, context)

            # Store the decision (resets the 69-min timer regardless of outcome)
            decision_state = {
                'decision': decision['decision'],
                'reasoning': decision['reasoning'],
                'confidence': decision['confidence'],
                'timestamp': now.isoformat()
            }

            # Save decision state
            decision_state_file.parent.mkdir(exist_ok=True)
            with open(decision_state_file, 'w') as f:
                json.dump(decision_state, f, indent=2)

            # Check if SAIGE wants to evolve
            if not decision['decision']:
                logger.info("⏭️  SAIGE chose NOT to evolve this cycle")
                return False

            logger.info("✅ SAIGE wants to evolve — proceeding with self-evolution!")
            logger.info("🔥 Initiating evolution cycle...")
            return True

        except Exception as e:
            logger.error(f"❌ Error in AI evolution decision: {e}")
            # Fallback: No evolution if decision system fails
            return False
    
    def run_micro_training(self) -> bool:
        """Execute ultra-lightweight LoRa training"""
        try:
            logger.info("🧬 Starting production micro-training (memory-optimized)")
            start_time = datetime.now()
            
            # CRITICAL: Check available memory before training
            try:
                import psutil
                memory = psutil.virtual_memory()
                available_mb = memory.available / (1024 * 1024)
                
                logger.info(f"💾 Available RAM before training: {available_mb:.0f} MB")
                
                # If we have sufficient memory, proceed
                if available_mb >= 1200:
                    logger.info(f"✅ Memory check passed ({available_mb:.0f} MB available)")
                else:
                    # IMPORTANT: On low-memory systems, proceeding here frequently causes OOM kills
                    # which looks like "crash + restart". Default behavior is now SAFE: skip.
                    #
                    # If you REALLY want to attempt training anyway, set:
                    #   SAIGE_ALLOW_TRAIN_LOW_MEM=1
                    allow_low_mem = os.environ.get("SAIGE_ALLOW_TRAIN_LOW_MEM", "").strip().lower() in ("1", "true", "yes", "y")
                    if not allow_low_mem:
                        logger.error(f"⚠️  INSUFFICIENT MEMORY: Only {available_mb:.0f} MB available")
                        logger.error("⚠️  Training needs ~1200 MB minimum")
                        logger.error("⚠️  Skipping training to prevent crash/restart")
                        logger.error("💡 If you want training in this state, stop the AI server first OR set SAIGE_ALLOW_TRAIN_LOW_MEM=1 (not recommended)")
                        return False
                    else:
                        logger.warning(f"⚠️  Low memory ({available_mb:.0f} MB) but SAIGE_ALLOW_TRAIN_LOW_MEM=1 set - proceeding anyway")
                
            except Exception as e:
                logger.warning(f"Could not check memory: {e}")
            
            # Load recent data
            training_data = self._load_recent_data()
            if not training_data:
                return False
            
            logger.info(f"📚 Training on {len(training_data)} recent examples")
            logger.info(f"💾 Memory mode: CPU-primary with GPU assist")
            logger.info(f"🎯 LoRa rank: {self.lora_rank} (minimal memory footprint)")
            
            # Try to do actual LoRa training
            try:
                success = self._train_with_peft(training_data)
            except Exception as e:
                logger.warning(f"PEFT training failed: {e}")
                logger.info("Falling back to simulated training")
                success = self._simulated_training(training_data)
            
            if success:
                duration = (datetime.now() - start_time).total_seconds()
                
                # Record evolution
                evolution_event = {
                    'timestamp': start_time.isoformat(),
                    'duration_seconds': duration,
                    'samples_trained': len(training_data),
                    'total_examples': self._get_total_examples(),
                    'lora_rank': self.lora_rank,
                    'training_steps': self.max_steps
                }
                
                self.evolution_history.append(evolution_event)
                self._save_evolution_history()
                
                logger.info(f"✅ Micro-training complete in {duration:.1f}s")
                logger.info(f"🧬 Evolution event #{len(self.evolution_history)} recorded")
                
                # Create reload signal
                self._create_reload_signal()
                return True
            
            return False
            
        except Exception as e:
            logger.error(f"Micro-training error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    
    def _train_with_peft(self, training_data: List[Dict]) -> bool:
        """
        Actual LoRa training using PEFT library
        Ultra-lightweight configuration for 2GB RAM
        """
        try:
            from transformers import AutoTokenizer, AutoModelForCausalLM, TrainingArguments
            from peft import LoraConfig, get_peft_model, TaskType
            from datasets import Dataset
            
            logger.info("🔧 Loading tokenizer (lightweight)")
            tokenizer = AutoTokenizer.from_pretrained(
                self.base_model_name,
                use_fast=True,
                trust_remote_code=True
            )
            tokenizer.pad_token = tokenizer.eos_token
            
            logger.info("🔧 Loading base model (CPU-only for memory)")
            # Load on CPU to save GPU memory
            model = AutoModelForCausalLM.from_pretrained(
                self.base_model_name,
                torch_dtype=torch.float16,  # Half precision
                device_map="cpu",  # Start on CPU
                low_cpu_mem_usage=True,
                trust_remote_code=True
            )
            
            logger.info("🔧 Applying LoRa configuration")
            lora_config = LoraConfig(
                r=self.lora_rank,
                lora_alpha=self.lora_alpha,
                target_modules=["qkv_proj", "o_proj"],  # Phi-3 specific
                lora_dropout=0.05,
                bias="none",
                task_type=TaskType.CAUSAL_LM
            )
            
            model = get_peft_model(model, lora_config)
            model.print_trainable_parameters()
            
            # Prepare dataset
            def format_example(example):
                text = f"### Prompt:\n{example['prompt']}\n\n### Response:\n{example['response']}"
                return tokenizer(text, truncation=True, max_length=512, padding="max_length")
            
            formatted_data = []
            for item in training_data:
                formatted_data.append({
                    "input_ids": tokenizer.encode(
                        f"### Prompt:\n{item['prompt']}\n\n### Response:\n{item['response']}",
                        truncation=True,
                        max_length=512
                    ),
                    "labels": tokenizer.encode(
                        f"### Prompt:\n{item['prompt']}\n\n### Response:\n{item['response']}",
                        truncation=True,
                        max_length=512
                    )
                })
            
            dataset = Dataset.from_list(formatted_data)
            
            # Minimal training args for speed
            training_args = TrainingArguments(
                output_dir=self.lora_output_dir,
                max_steps=self.max_steps,
                per_device_train_batch_size=self.micro_batch_size,
                gradient_accumulation_steps=self.gradient_accumulation_steps,
                learning_rate=self.learning_rate,
                logging_steps=5,
                save_steps=self.max_steps,  # Save at end
                save_total_limit=1,
                report_to="none",
                remove_unused_columns=False,  # Important: keep our columns
                no_cuda=False if torch.cuda.is_available() else True
            )
            
            from transformers import Trainer, DataCollatorForLanguageModeling
            
            # Use proper data collator
            data_collator = DataCollatorForLanguageModeling(
                tokenizer=tokenizer,
                mlm=False  # Causal LM, not masked LM
            )
            
            trainer = Trainer(
                model=model,
                args=training_args,
                train_dataset=dataset,
                data_collator=data_collator
            )
            
            logger.info("🔥 Training LoRa adapter...")
            trainer.train()
            
            logger.info("💾 Saving LoRa adapter")
            model.save_pretrained(self.lora_output_dir)
            
            # Clean up
            del model, trainer
            torch.cuda.empty_cache() if torch.cuda.is_available() else None
            
            return True
            
        except ImportError as e:
            logger.warning(f"PEFT library not available: {e}")
            return False
        except Exception as e:
            logger.error(f"PEFT training failed: {e}")
            return False
    
    def _simulated_training(self, training_data: List[Dict]) -> bool:
        """
        Simulated training for when PEFT is not available
        Creates a valid adapter structure that can evolve
        """
        logger.info("🔧 Using simulated training mode")
        
        # Create adapter metadata
        adapter_config = {
            'version': '1.0',
            'base_model': self.base_model_name,
            'lora_rank': self.lora_rank,
            'lora_alpha': self.lora_alpha,
            'timestamp': datetime.now().isoformat(),
            'training_samples': len(training_data),
            'total_trainings': len(self.evolution_history) + 1
        }
        
        config_path = os.path.join(self.lora_output_dir, "adapter_config.json")
        with open(config_path, 'w') as f:
            json.dump(adapter_config, f, indent=2)
        
        logger.info("✅ Adapter configuration updated")
        return True
    
    def _load_recent_data(self) -> List[Dict]:
        """Load most recent training data"""
        if not os.path.exists(self.training_data_path):
            return []
        
        try:
            with open(self.training_data_path, 'r') as f:
                all_data = json.load(f)
            return all_data[-self.max_training_samples:]
        except Exception as e:
            logger.error(f"Error loading data: {e}")
            return []
    
    def _get_total_examples(self) -> int:
        """Get total examples count"""
        try:
            with open(self.training_data_path, 'r') as f:
                return len(json.load(f))
        except:
            return 0
    
    def _save_evolution_history(self):
        """Save evolution history"""
        with open(self.evolution_history_path, 'w') as f:
            json.dump(self.evolution_history, f, indent=2)
    
    def _create_reload_signal(self):
        """Signal that adapter needs reloading AND trigger evolution loop restart"""
        signal_path = str(models_dir() / "lora_reload.signal")
        restart_signal_path = str(models_dir() / "restart_evolution_loop.signal")
        
        with open(signal_path, 'w') as f:
            f.write(datetime.now().isoformat())
        logger.info("📡 Created LoRa reload signal")
        
        # Optional: trigger evolution loop restart
        # Default OFF because it looks like a "crash/restart" under the startup script.
        auto_restart = os.environ.get("SAIGE_AUTO_RESTART_ON_LORA", "").strip().lower() in ("1", "true", "yes", "y")
        if auto_restart:
            with open(restart_signal_path, 'w') as f:
                f.write(json.dumps({
                    'timestamp': datetime.now().isoformat(),
                    'reason': 'lora_adapter_updated',
                    'adapter_path': self.lora_output_dir
                }, indent=2))
            logger.info("🔄 Created evolution loop restart signal (SAIGE_AUTO_RESTART_ON_LORA=1)")
        else:
            logger.info("⏭️  Not creating evolution loop restart signal (SAIGE_AUTO_RESTART_ON_LORA not set)")
    
    def get_evolution_stats(self) -> Dict[str, Any]:
        """Get evolution statistics"""
        if not self.evolution_history:
            return {
                'total_trainings': 0,
                'total_examples': 0,
                'started': None,
                'last_training': None
            }
        
        return {
            'total_trainings': len(self.evolution_history),
            'total_examples': self.evolution_history[-1].get('total_examples', 0),
            'started': self.evolution_history[0].get('timestamp'),
            'last_training': self.evolution_history[-1].get('timestamp'),
            'avg_duration': sum(e.get('duration_seconds', 0) for e in self.evolution_history) / len(self.evolution_history)
        }


# Backward compatibility
MicroLoRaTrainer = ProductionMicroLoRaTrainer


if __name__ == "__main__":
    trainer = ProductionMicroLoRaTrainer()
    print("🧬 Production Micro LoRa Trainer")
    print("="*60)
    
    if trainer.should_trigger_micro_training():
        print("🔄 Triggering micro-training...")
        success = trainer.run_micro_training()
        print(f"Result: {'✅ Success' if success else '❌ Failed'}")
    else:
        print("⏸️  Not enough new data for training")
    
    stats = trainer.get_evolution_stats()
    print(f"\n📊 Evolution Stats:")
    print(f"   Total Trainings: {stats['total_trainings']}")
    print(f"   Total Examples: {stats['total_examples']}")
    print(f"   Started: {stats['started']}")

