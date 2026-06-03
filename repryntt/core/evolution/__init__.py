"""
repryntt.core.evolution — QLoRA self-evolution pipeline.

Disabled by default. When enabled:
    1. Collect high-quality prompt/response training pairs during operation
    2. Quality-gate the data (score by outcome)
    3. Wait for maintenance window (low-activity)
    4. Stop llama-server → free GPU memory
    5. Fine-tune with QLoRA (4-bit quantized LoRA adapters)
    6. Merge adapter → convert to GGUF
    7. Restart llama-server with evolved model
    8. Validate → rollback if sanity checks fail

Migration source:
    - SAIGE/scripts/self_evolution_manager.py (~500 lines — orchestrator)
    - SAIGE/scripts/run_qlora_training.py (~300 lines — training runner)
    - SAIGE/scripts/micro_lora_trainer_production.py (~400 lines — production trainer)
    - SAIGE/scripts/micro_lora_trainer.py (~300 lines — base trainer class)
    - SAIGE/scripts/qlora_trainer.py (~500 lines — full QLoRA)
    - SAIGE/scripts/qlora_peft_to_gguf.py (~300 lines — adapter merge + GGUF)
    - SAIGE/brain/training_quality_gate.py (~200 lines — data quality scoring)
    - SAIGE/brain/evolution_bootstrap_manager.py (~300 lines — bootstrap updates)
"""
