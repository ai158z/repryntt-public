"""
Repryntt — Autonomous AI Framework

Hormone-driven self-prompting with three-tier model routing and self-evolution.
Runs on edge hardware (Jetson Orin Nano) through cloud GPU clusters.

Architecture:
    repryntt.core       — Heartbeat loop, hormone system, memory, self-evolution, identity
    repryntt.routing    — Three-tier AI model routing (edge/cloud/heavy GPU)
    repryntt.trading    — Trading engine, whale monitor, signals, execution
    repryntt.search     — Knowledge router, data feeders, web research
    repryntt.economy    — Proof-of-Power blockchain, wallets, tokenization
    repryntt.agents     — Swarm orchestration, departments, persistent agents
    repryntt.tools      — Clean tool registry, discovery, chain execution
    repryntt.web        — API endpoints, chat server, web interfaces
    repryntt.hardware   — Voice (TTS/STT), vision (cameras), ROS2
    repryntt.comms      — Channel gateway, webhooks, auth (OpenClaw integration)
"""

__version__ = "0.6.0"
__author__ = "Nate"

# Fix Windows cp1252 console encoding (must run before any logging)
from repryntt.platform_utils import fix_windows_encoding as _fix_enc
_fix_enc()
del _fix_enc
