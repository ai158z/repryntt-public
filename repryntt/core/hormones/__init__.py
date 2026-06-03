"""
repryntt.core.hormones — Algorithmic hormone system.

Scientifically-grounded artificial neuroendocrine system:
    - 8 neurochemicals (dopamine, serotonin, norepinephrine, cortisol, oxytocin, endorphins, GABA, acetylcholine)
    - Schultz RPE / TD-Learning for dopamine-driven prioritization
    - Lövheim's Cube mapping neurochemicals → 8 primary emotions
    - Homeostatic decay with per-chemical enzymatic clearance
    - Cañamero deficit-driven motivation
    - Solomon-Corbit opponent process (habituation + withdrawal)
    - Panksepp's 7 affective circuits (SEEKING, RAGE, FEAR, LUST, CARE, PLAY, PANIC/GRIEF)

Migration source:
    - SAIGE/brain/algorithmic_hormone_system.py (~800 lines)
    - SAIGE/jarvis_consciousness.py (~400 lines — persistent emotional state)
    - SAIGE/jarvis_learning.py (~500 lines — experience-weighted behavioral memory)
"""
