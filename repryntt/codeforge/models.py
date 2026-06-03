"""
CodeForge Data Models — Project specs, modules, results, quality reports.

Every forge project flows through: Spec → Architect → Generate → Test → Validate → Package
Each stage produces a typed result that feeds the next stage.
"""

import time
import uuid
import json
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Optional, Any
from pathlib import Path


class ProjectType(str, Enum):
    """What kind of project this is — determines pipeline behavior."""
    LIBRARY = "library"           # standalone package/module (pip, npm)
    CLI = "cli"                   # command-line tool
    API = "api"                   # backend API (Flask, FastAPI, Express)
    WEBAPP = "webapp"             # frontend web app (React, Vue, Svelte)
    FULLSTACK = "fullstack"       # frontend + backend + optional DB
    MOBILE = "mobile"             # React Native, Flutter (no runtime test)
    SAAS = "saas"                 # fullstack + auth + payments + multi-tenancy
    AUTOMATION = "automation"     # scripts, bots, data pipelines


class ForgeStatus(str, Enum):
    """Pipeline status for projects and modules."""
    QUEUED = "queued"
    SPECIFYING = "specifying"
    ARCHITECTING = "architecting"
    GENERATING = "generating"
    TESTING = "testing"
    VALIDATING = "validating"
    PACKAGING = "packaging"
    COMPLETED = "completed"
    FAILED = "failed"
    FIX_ITERATING = "fix_iterating"    # retrying after test failure
    AWAITING_SWARM = "awaiting_swarm"  # waiting for distributed workers


class ModuleStatus(str, Enum):
    PENDING = "pending"
    GENERATING = "generating"
    GENERATED = "generated"
    TESTING = "testing"
    PASSED = "passed"
    FAILED = "failed"
    FIX_RETRY = "fix_retry"
    # Module belongs to a language we can't probe in this installation
    # (e.g. JS/TS without Node). Build continues; this module is left as a
    # text artifact for the operator to handle.
    SKIPPED = "skipped"


class SwarmNodeRole(str, Enum):
    """Role a node plays in a distributed forge job."""
    COORDINATOR = "coordinator"   # owns the project, merges results
    GENERATOR = "generator"       # writes code for assigned modules
    TESTER = "tester"             # runs tests on generated code
    REVIEWER = "reviewer"         # quality review of generated code


@dataclass
class ForgeModule:
    """A single file/module within a forge project."""
    module_id: str = ""
    filename: str = ""              # e.g. "src/utils/parser.py"
    language: str = "python"
    description: str = ""           # what this module does
    dependencies: List[str] = field(default_factory=list)  # other module_ids it imports
    interfaces: str = ""            # public API spec (class/function signatures)
    implementation: str = ""        # generated source code
    test_code: str = ""             # generated test code
    test_output: str = ""           # stdout/stderr from running tests
    status: str = ModuleStatus.PENDING.value
    retries: int = 0
    max_retries: int = 3
    assigned_node: str = ""         # node_id that's generating this (swarm mode)
    quality_score: float = 0.0

    def __post_init__(self):
        if not self.module_id:
            self.module_id = f"mod-{uuid.uuid4().hex[:8]}"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ForgeModule":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class QualityReport:
    """Quality assessment for a forge project."""
    syntax_clean: bool = False
    all_tests_pass: bool = False
    tests_total: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    security_issues: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    total_lines: int = 0
    total_files: int = 0
    complexity_score: float = 0.0   # 0-100, lower is simpler
    overall_score: float = 0.0      # 0-100 composite

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "QualityReport":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class SwarmTask:
    """A task assigned to a remote node in swarm mode."""
    task_id: str = ""
    project_id: str = ""
    module_id: str = ""
    role: str = SwarmNodeRole.GENERATOR.value
    node_id: str = ""
    status: str = "assigned"   # assigned, in_progress, completed, failed
    result: str = ""           # generated code or test output
    assigned_at: float = 0.0
    completed_at: float = 0.0
    benchmark_score: float = 0.0  # node's benchmark score when assigned

    def __post_init__(self):
        if not self.task_id:
            self.task_id = f"task-{uuid.uuid4().hex[:8]}"
        if not self.assigned_at:
            self.assigned_at = time.time()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SwarmTask":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ServiceDefinition:
    """A backend service required by the project (database, cache, queue, etc.)."""
    name: str = ""                  # e.g. "postgres", "redis", "rabbitmq"
    image: str = ""                 # Docker image, e.g. "postgres:16-alpine"
    ports: List[str] = field(default_factory=list)   # ["5432:5432"]
    env_vars: Dict[str, str] = field(default_factory=dict)  # POSTGRES_PASSWORD=test
    health_check: str = ""          # command to verify service is ready
    volumes: List[str] = field(default_factory=list) # persistent mounts

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ServiceDefinition":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class ForgeProject:
    """A complete code forge project — the top-level entity."""
    project_id: str = ""
    name: str = ""
    description: str = ""           # original natural language request
    language: str = "python"
    framework: str = ""             # e.g. "flask", "fastapi", "react"
    project_type: str = ProjectType.LIBRARY.value  # library, api, webapp, fullstack, saas...
    constraints: List[str] = field(default_factory=list)
    status: str = ForgeStatus.QUEUED.value
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0

    # Full-stack: multiple languages/services
    services: List[ServiceDefinition] = field(default_factory=list)  # docker services needed
    frontend_framework: str = ""    # e.g. "react", "vue", "svelte" (for fullstack/saas)
    backend_framework: str = ""     # e.g. "fastapi", "express" (for fullstack/saas)
    database: str = ""              # e.g. "postgres", "mongodb", "sqlite"

    # Stage outputs
    spec: Dict[str, Any] = field(default_factory=dict)         # structured spec from LLM
    architecture: Dict[str, Any] = field(default_factory=dict)  # file tree + interfaces
    modules: List[ForgeModule] = field(default_factory=list)
    quality: Optional[QualityReport] = None
    package_path: str = ""          # path to final deliverable

    # Pipeline config
    provider: str = ""              # "nvidia", "local", "anthropic" — which LLM to use
    model: str = ""                 # specific model override
    max_retries: int = 3
    current_stage: str = "spec"     # which pipeline stage we're in

    # Swarm config
    swarm_enabled: bool = False
    coordinator_node: str = ""      # node_id of the coordinator
    swarm_tasks: List[SwarmTask] = field(default_factory=list)
    min_benchmark_score: float = 60.0  # minimum score to accept contributors

    # Tracking
    api_calls: int = 0
    total_tokens: int = 0
    error_log: List[str] = field(default_factory=list)
    stage_timings: Dict[str, float] = field(default_factory=dict)  # stage → seconds

    # Validation outcomes — populated in _stage_validate
    test_results: Optional[Dict[str, Any]] = None    # {ok, summary, stdout_tail}
    critic_verdict: Optional[Dict[str, Any]] = None  # {pass, concerns, specialist, universal}

    def __post_init__(self):
        if not self.project_id:
            # Human-readable project IDs: forge-<name_slug>-<short_hash>
            # e.g., forge-rss_reader-a3f1 instead of forge-5d8dc457
            import re as _re
            slug = self.name or self.description or ""
            slug = slug.lower().strip()[:40]
            slug = _re.sub(r'[^a-z0-9]+', '_', slug).strip('_')
            if slug:
                self.project_id = f"forge-{slug}-{uuid.uuid4().hex[:4]}"
            else:
                self.project_id = f"forge-{uuid.uuid4().hex[:8]}"
        if not self.created_at:
            self.created_at = time.time()

    def to_dict(self) -> dict:
        d = asdict(self)
        # Convert nested objects
        d["modules"] = [m.to_dict() if isinstance(m, ForgeModule) else m for m in self.modules]
        d["quality"] = self.quality.to_dict() if self.quality else None
        d["swarm_tasks"] = [t.to_dict() if isinstance(t, SwarmTask) else t for t in self.swarm_tasks]
        d["services"] = [s.to_dict() if isinstance(s, ServiceDefinition) else s for s in self.services]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ForgeProject":
        modules = [ForgeModule.from_dict(m) if isinstance(m, dict) else m
                    for m in d.pop("modules", [])]
        quality_data = d.pop("quality", None)
        quality = QualityReport.from_dict(quality_data) if isinstance(quality_data, dict) else None
        swarm_tasks = [SwarmTask.from_dict(t) if isinstance(t, dict) else t
                        for t in d.pop("swarm_tasks", [])]
        services = [ServiceDefinition.from_dict(s) if isinstance(s, dict) else s
                     for s in d.pop("services", [])]
        valid = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        proj = cls(**valid)
        proj.modules = modules
        proj.quality = quality
        proj.swarm_tasks = swarm_tasks
        proj.services = services
        return proj

    def save(self, base_dir: Path):
        """Persist project state to disk."""
        project_dir = base_dir / self.project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        state_file = project_dir / "project.json"
        state_file.write_text(json.dumps(self.to_dict(), indent=2, default=str))

    @classmethod
    def load(cls, project_dir: Path) -> "ForgeProject":
        state_file = project_dir / "project.json"
        if not state_file.exists():
            raise FileNotFoundError(f"No project.json in {project_dir}")
        return cls.from_dict(json.loads(state_file.read_text()))

    @property
    def progress_pct(self) -> float:
        """Estimate overall progress 0-100."""
        stage_weights = {
            "spec": 10, "architect": 20, "generate": 50,
            "test": 70, "validate": 85, "package": 95,
        }
        base = stage_weights.get(self.current_stage, 0)
        if self.status == ForgeStatus.COMPLETED.value:
            return 100.0
        if self.status == ForgeStatus.FAILED.value:
            return base
        # Add module-level progress within generate stage
        if self.current_stage == "generate" and self.modules:
            done = sum(1 for m in self.modules if m.status in
                       (ModuleStatus.GENERATED.value, ModuleStatus.PASSED.value))
            module_pct = done / len(self.modules) if self.modules else 0
            return base + (stage_weights.get("test", 70) - base) * module_pct
        return float(base)


@dataclass
class BenchmarkResult:
    """Result of a coding benchmark test for a node/model."""
    node_id: str = ""
    model_name: str = ""
    provider: str = ""
    score: float = 0.0              # 0-100
    tasks_attempted: int = 0
    tasks_passed: int = 0
    avg_response_time: float = 0.0  # seconds
    tested_at: float = 0.0
    language_scores: Dict[str, float] = field(default_factory=dict)  # lang → score
    expires_at: float = 0.0         # benchmark valid until

    def __post_init__(self):
        if not self.tested_at:
            self.tested_at = time.time()
        if not self.expires_at:
            self.expires_at = self.tested_at + 86400  # valid for 24h

    @property
    def is_valid(self) -> bool:
        return time.time() < self.expires_at

    @property
    def passed(self) -> bool:
        return self.score >= 60.0  # minimum to contribute

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "BenchmarkResult":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
