"""
repryntt.core.heartbeat — The self-prompting autonomous reasoning loop.

This is the engine that makes an AI instance alive. Each tick:
    1. Read stimulus from feeders
    2. Update hormones from stimulus
    3. Compute emotions (Lövheim's Cube + Panksepp circuits)
    4. Generate thoughts from emotional state
    5. Decide and execute tasks (or generate new ones)
    6. Store memories, update identity
    7. Collect training data for self-evolution

Migration source:
    - SAIGE/scripts/saige_evolution_loop.py (4,037 lines — SAIGEEvolutionLoop class)
    - SAIGE/brain/morning_startup_prompt.py (~300 lines — task generation)
    - SAIGE/brain/evolution_skill_loader.py (~200 lines — SKILL.md loading)
"""
