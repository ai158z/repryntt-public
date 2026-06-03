"""
CodeForge — Autonomous Code Factory for REPRYNTT

A Blitzy-inspired code generation pipeline that:
1. Parses natural language into structured project specs
2. Architects file/module structure with interfaces
3. Generates production-quality code via LLM (API or local)
4. Auto-generates and runs tests
5. Validates syntax, security, and quality
6. Packages tested deliverables with README and quality report

Supports:
- Dual LLM: NVIDIA API, Anthropic, OpenAI-compatible, or local llama.cpp
- Agent swarm: distribute module generation across P2P network nodes
- Quality gating: nodes must pass a coding benchmark to contribute
- Fix-iterate: automatic retry loop when tests fail
- Content-addressed artifacts: packages stored in REPRYNTT's P2P store
"""

from .models import (
    ForgeProject, ForgeModule, ForgeStatus, ModuleStatus,
    QualityReport, SwarmTask, SwarmNodeRole, BenchmarkResult,
    ProjectType, ServiceDefinition,
)
from .forge import CodeForge, get_forge
from .benchmark import run_benchmark, get_cached_benchmark, save_benchmark
from .swarm import ForgeSwarm, get_swarm
from .validator import check_syntax, scan_security, build_quality_report
from .packager import package_project, register_artifact
from .environments import ServiceEnvironment, resolve_services

__all__ = [
    # Models
    "ForgeProject", "ForgeModule", "ForgeStatus", "ModuleStatus",
    "QualityReport", "SwarmTask", "SwarmNodeRole", "BenchmarkResult",
    "ProjectType", "ServiceDefinition",
    # Forge engine
    "CodeForge", "get_forge",
    # Swarm
    "ForgeSwarm", "get_swarm",
    # Benchmark
    "run_benchmark", "get_cached_benchmark", "save_benchmark",
    # Validator
    "check_syntax", "scan_security", "build_quality_report",
    # Packager
    "package_project", "register_artifact",
    # Environments
    "ServiceEnvironment", "resolve_services",
]

__version__ = "1.0.0"
