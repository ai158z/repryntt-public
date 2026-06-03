"""
repryntt.core.identity — Bootstrap identity management.

Each repryntt instance has a unique identity defined by markdown bootstrap files:
    SPIRIT.md   — Core identity, personality, drives, values
    PROFILE.md  — Personality canvas (evolves over time)
    OPERATOR.md — Operator profile and preferences
    PULSE.md    — Drive priorities (trade, serve, learn, evolve, introspect)
    PROTOCOL.md — Session rules, memory constraints, safety
    TOOLKIT.md  — Hardware specs, environment details
    RECALL.md   — Long-term curated memory
    GENESIS.md  — Birth certificate
    TRADING.md  — Trading context and rules
    STARTUP.md  — Boot sequence

These files live in /bootstrap/ at the project root and are loaded every heartbeat cycle.

Migration source:
    - SAIGE/brain/bootstrap/*.md (10 files)
    - SAIGE/saige_identity.py (~200 lines — machine identity per installation)
"""
