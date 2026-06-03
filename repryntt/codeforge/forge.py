"""
CodeForge Orchestrator — Runs the full Spec→Architect→Generate→Test→Validate→Package pipeline.

This is the central engine. It can be invoked by:
1. Jarvis via tools (forge_project)
2. Web API (/api/forge/start)
3. Swarm coordinator (distributed mode)
4. CLI for testing

Each stage is idempotent — if the forge crashes mid-pipeline, it can resume
from the last saved state.
"""

import json
import os
import sys
import time
import logging
import threading
from typing import Optional, Dict, List, Any
from pathlib import Path

from .models import (
    ForgeProject, ForgeModule, ForgeStatus, ModuleStatus,
    QualityReport, SwarmTask,
)
from .generator import (
    _load_ai_config, _resolve_provider,
    generate_spec, generate_architecture,
    generate_module_code, generate_test_code,
    fix_module_code, generate_readme, generate_ci_config,
    _postprocess_code,
)
from .tester import run_all_tests
from .validator import validate_module, build_quality_report
from .packager import package_project, register_artifact
from .agent_loop import (
    generate_module_iteratively,
    generate_tests_iteratively,
    repair_cross_module_breakage,
)
from . import runtimes as _runtimes

logger = logging.getLogger("codeforge.forge")

# ── Storage ──
FORGE_BASE = Path.home() / ".repryntt" / "workspace" / "projects" / "codeforge"
FORGE_BASE.mkdir(parents=True, exist_ok=True)


class CodeForge:
    """
    The main CodeForge engine. Manages projects and runs the pipeline.
    Thread-safe — swarm mode runs concurrently; non-swarm serializes API calls.
    """

    # Statuses that mean a pipeline thread should be actively working.
    # Anything in this set at startup (when no thread is alive yet) is stale.
    _ACTIVE_STATUSES: tuple = (
        ForgeStatus.QUEUED.value, ForgeStatus.SPECIFYING.value,
        ForgeStatus.ARCHITECTING.value, ForgeStatus.GENERATING.value,
        ForgeStatus.TESTING.value, ForgeStatus.FIX_ITERATING.value,
        ForgeStatus.VALIDATING.value, ForgeStatus.PACKAGING.value,
    )

    def __init__(self):
        self._projects: Dict[str, ForgeProject] = {}
        self._lock = threading.Lock()
        self._running_jobs: Dict[str, threading.Thread] = {}
        # Serialization: when a non-swarm project is running, block other
        # projects AND signal the agent heartbeat to defer API calls.
        self._active_project_id: Optional[str] = None
        self._pipeline_lock = threading.Lock()  # one pipeline at a time (non-swarm)
        self._load_existing_projects()
        # Daemon may have restarted mid-build, leaving stale "generating" /
        # "fix_iterating" / etc. records on disk with no live thread to drive
        # them. Reap on startup so they don't block new builds forever.
        self._reap_stale_active()

    def _load_existing_projects(self):
        """Load any previously saved projects from disk."""
        if not FORGE_BASE.exists():
            return
        for proj_dir in FORGE_BASE.iterdir():
            if not proj_dir.is_dir():
                continue
            state_file = proj_dir / "project.json"
            if state_file.exists():
                try:
                    proj = ForgeProject.load(proj_dir)
                    self._projects[proj.project_id] = proj
                except Exception as e:
                    logger.warning(f"Failed to load project {proj_dir.name}: {e}")

    def _reap_stale_active(self) -> int:
        """Mark stale active projects as cancelled.

        Called once at __init__. A project is "stale" if it loaded from disk
        in an active status (specifying, architecting, generating, ...) but
        no live `_running_jobs` thread is driving it. That happens when the
        daemon died or restarted mid-pipeline.

        Returns the number of projects reaped.
        """
        reaped = 0
        for pid, proj in list(self._projects.items()):
            if proj.status not in self._ACTIVE_STATUSES:
                continue
            t = self._running_jobs.get(pid)
            if t is not None and t.is_alive():
                continue
            stuck_at = proj.status
            # ForgeStatus has no CANCELLED — reuse FAILED, same downstream effect.
            proj.status = ForgeStatus.FAILED.value
            proj.completed_at = time.time()
            if not hasattr(proj, "error_log") or proj.error_log is None:
                proj.error_log = []
            proj.error_log.append(
                f"[reap] cancelled at startup — pipeline thread did not survive "
                f"daemon restart. Was {stuck_at!r}, never resumed."
            )
            try:
                self._save_project(proj)
            except Exception as e:
                logger.warning(f"reap: could not save {pid}: {e}")
            reaped += 1
        if reaped:
            logger.warning(
                f"🧹 CodeForge startup: reaped {reaped} stale active project(s) "
                f"left over from a previous daemon session."
            )
        if self._active_project_id and self._projects.get(self._active_project_id):
            ap = self._projects[self._active_project_id]
            if ap.status not in self._ACTIVE_STATUSES:
                self._active_project_id = None
        return reaped

    def _save_project(self, project: ForgeProject):
        """Persist project state."""
        project.updated_at = time.time()
        project.save(FORGE_BASE)

    # ──────────────────────────────────────────────────────────────
    # PUBLIC API
    # ──────────────────────────────────────────────────────────────

    def start_project(self, description: str, provider: str = "",
                      model: str = "", swarm_enabled: bool = False,
                      min_benchmark: float = 60.0) -> ForgeProject:
        """
        Create a new forge project and start the pipeline in a background thread.
        Returns the project immediately (status=queued).
        """
        # Block if a non-swarm project is already running
        if not swarm_enabled and self._active_project_id:
            active = self._projects.get(self._active_project_id)
            if active and active.status in (
                ForgeStatus.SPECIFYING.value, ForgeStatus.ARCHITECTING.value,
                ForgeStatus.GENERATING.value, ForgeStatus.TESTING.value,
                ForgeStatus.FIX_ITERATING.value, ForgeStatus.VALIDATING.value,
                ForgeStatus.PACKAGING.value,
            ):
                raise RuntimeError(
                    f"Another forge project is already running "
                    f"({self._active_project_id}). In non-swarm mode only "
                    f"one project can run at a time. Cancel it first or "
                    f"wait for it to finish."
                )

        project = ForgeProject(
            description=description,
            provider=provider,
            model=model,
            swarm_enabled=swarm_enabled,
            min_benchmark_score=min_benchmark,
        )

        with self._lock:
            self._projects[project.project_id] = project
        self._save_project(project)

        # Run pipeline in background
        t = threading.Thread(
            target=self._run_pipeline,
            args=(project.project_id,),
            daemon=True,
            name=f"forge-{project.project_id}",
        )
        self._running_jobs[project.project_id] = t
        t.start()

        logger.info(f"🔨 Started forge project: {project.project_id}")
        return project

    def get_project(self, project_id: str) -> Optional[ForgeProject]:
        """Get a project by ID."""
        return self._projects.get(project_id)

    def list_projects(self) -> List[Dict]:
        """List all projects with summary info."""
        return [
            {
                "project_id": p.project_id,
                "name": p.name or "(unnamed)",
                "description": p.description[:100],
                "status": p.status,
                "progress": p.progress_pct,
                "language": p.language,
                "modules": len(p.modules),
                "quality_score": p.quality.overall_score if p.quality else None,
                "created_at": p.created_at,
                "swarm_enabled": p.swarm_enabled,
            }
            for p in sorted(self._projects.values(),
                            key=lambda x: x.created_at, reverse=True)
        ]

    def get_project_detail(self, project_id: str) -> Optional[Dict]:
        """Get full project details."""
        p = self._projects.get(project_id)
        if not p:
            return None
        return p.to_dict()

    def cancel_project(self, project_id: str) -> bool:
        """Cancel a running project."""
        p = self._projects.get(project_id)
        if not p:
            return False
        if p.status in (ForgeStatus.COMPLETED.value, ForgeStatus.FAILED.value):
            return False
        p.status = ForgeStatus.FAILED.value
        p.error_log.append("Cancelled by operator")
        self._save_project(p)
        if self._active_project_id == project_id:
            self._active_project_id = None
        return True

    def is_forge_active(self) -> bool:
        """
        Returns True if a non-swarm forge project is actively running
        and consuming the shared API. Used by the agent heartbeat to
        defer its own API calls so they don't compete.
        """
        pid = self._active_project_id
        if not pid:
            return False
        p = self._projects.get(pid)
        if not p:
            self._active_project_id = None
            return False
        if p.swarm_enabled:
            return False  # swarm mode has its own capacity
        active_statuses = (
            ForgeStatus.SPECIFYING.value, ForgeStatus.ARCHITECTING.value,
            ForgeStatus.GENERATING.value, ForgeStatus.TESTING.value,
            ForgeStatus.FIX_ITERATING.value, ForgeStatus.VALIDATING.value,
            ForgeStatus.PACKAGING.value,
        )
        if p.status in active_statuses:
            return True
        # Stale — project finished, clear the flag
        self._active_project_id = None
        return False

    # ──────────────────────────────────────────────────────────────
    # PIPELINE
    # ──────────────────────────────────────────────────────────────

    def _run_pipeline(self, project_id: str):
        """Run the full forge pipeline for a project."""
        project = self._projects.get(project_id)
        if not project:
            return

        # Non-swarm: serialize pipelines so they don't compete for API
        acquired = False
        if not project.swarm_enabled:
            acquired = self._pipeline_lock.acquire(timeout=600)
            if not acquired:
                project.status = ForgeStatus.FAILED.value
                project.error_log.append(
                    "Could not acquire pipeline lock — another project is running"
                )
                self._save_project(project)
                return
            self._active_project_id = project_id

        try:
            # Resolve LLM provider — operator can specify both provider and
            # model per project. Empty values fall back to ai_config defaults.
            config = _load_ai_config()
            provider_info = _resolve_provider(
                config, project.provider, model_override=project.model
            )

            if not provider_info.get("endpoint"):
                project.status = ForgeStatus.FAILED.value
                project.error_log.append("No LLM endpoint configured")
                self._save_project(project)
                return

            # Stage 1: Spec
            self._stage_spec(project, provider_info)
            if project.status == ForgeStatus.FAILED.value:
                return

            # Stage 2: Architecture
            self._stage_architect(project, provider_info)
            if project.status == ForgeStatus.FAILED.value:
                return

            # Stage 3: Generate
            self._stage_generate(project, provider_info)
            if project.status == ForgeStatus.FAILED.value:
                return

            # Stage 4: Test
            self._stage_test(project, provider_info)
            # Don't return on failure — continue to validate what we have

            # Stage 5: Validate
            self._stage_validate(project)

            # Stage 6: Package
            self._stage_package(project, provider_info)

        except Exception as e:
            logger.error(f"Pipeline failed for {project_id}: {e}", exc_info=True)
            project.status = ForgeStatus.FAILED.value
            project.error_log.append(f"Pipeline exception: {str(e)}")
            self._save_project(project)
        finally:
            self._running_jobs.pop(project_id, None)
            if self._active_project_id == project_id:
                self._active_project_id = None
            if acquired:
                try:
                    self._pipeline_lock.release()
                except RuntimeError:
                    pass

    def _stage_spec(self, project: ForgeProject, provider_info: dict):
        """Stage 1: Generate structured spec from description."""
        stage_start = time.time()
        project.status = ForgeStatus.SPECIFYING.value
        project.current_stage = "spec"
        self._save_project(project)

        logger.info(f"📋 [{project.project_id}] Stage 1: Generating spec...")

        max_spec_retries = 3
        spec = None
        for attempt in range(1, max_spec_retries + 1):
            spec = generate_spec(project.description, provider_info)
            project.api_calls += 1
            if spec:
                break
            if attempt < max_spec_retries:
                logger.warning(f"📋 [{project.project_id}] Spec attempt {attempt}/{max_spec_retries} failed, retrying in 15s...")
                project.error_log.append(f"Spec attempt {attempt} failed, retrying...")
                self._save_project(project)
                time.sleep(15)

        if not spec:
            project.status = ForgeStatus.FAILED.value
            project.error_log.append(f"Failed to generate spec after {max_spec_retries} attempts")
            self._save_project(project)
            return

        project.spec = spec
        project.name = spec.get("name", project.project_id)
        project.language = spec.get("language", "python")
        project.framework = spec.get("framework", "")
        project.project_type = spec.get("project_type", "library")
        project.frontend_framework = spec.get("frontend_framework", "")
        project.backend_framework = spec.get("backend_framework", "")
        project.database = spec.get("database", "")
        if project.database and project.database.lower() == "none":
            project.database = ""
        project.stage_timings["spec"] = time.time() - stage_start
        self._save_project(project)
        logger.info(f"📋 [{project.project_id}] Spec complete: {project.name} "
                     f"({project.language}, type={project.project_type})")

    def _stage_architect(self, project: ForgeProject, provider_info: dict):
        """Stage 2: Generate architecture and module definitions."""
        stage_start = time.time()
        project.status = ForgeStatus.ARCHITECTING.value
        project.current_stage = "architect"
        self._save_project(project)

        logger.info(f"🏗️ [{project.project_id}] Stage 2: Architecting...")

        # Retry architecture generation (API timeouts are transient on free tier)
        max_arch_retries = 3
        arch = None
        for attempt in range(1, max_arch_retries + 1):
            arch = generate_architecture(project, provider_info)
            project.api_calls += 1
            if arch:
                break
            if attempt < max_arch_retries:
                logger.warning(f"🏗️ [{project.project_id}] Architecture attempt {attempt}/{max_arch_retries} failed, retrying in 15s...")
                project.error_log.append(f"Architecture attempt {attempt} failed, retrying...")
                self._save_project(project)
                time.sleep(15)

        if not arch:
            project.status = ForgeStatus.FAILED.value
            project.error_log.append(f"Failed to generate architecture after {max_arch_retries} attempts")
            self._save_project(project)
            return

        project.architecture = arch

        # ── Architecture sanity-check pass ──
        # Cheap LLM-judge call: catch obviously-bad plans before we spend the
        # whole generate stage on them. If concerns are found, do one focused
        # re-architect attempt with those concerns appended as guidance.
        try:
            from .generator import judge_architecture
            # Pass ai_config so the judge can route through critic_provider
            # if the operator has configured one (Fix #7: hybrid deployment).
            verdict = judge_architecture(project, provider_info, config=_load_ai_config())
            project.api_calls += 1
            if verdict.get("concerns"):
                project.error_log.append(
                    "architecture judge concerns: "
                    + " | ".join(verdict["concerns"][:3])
                )
            logger.info(
                f"🏗️ [{project.project_id}] architecture-judge: ok={verdict.get('ok')}, "
                f"concerns={len(verdict.get('concerns', []))}, "
                f"reasoning={verdict.get('reasoning', '')[:120]!r}"
            )
            # One re-architect attempt if the judge said no
            if not verdict.get("ok", True) and verdict.get("concerns"):
                logger.warning(
                    f"🏗️ [{project.project_id}] architecture judge said NO — "
                    f"one re-architect attempt with concerns fed back"
                )
                # Append concerns to the next arch call. The simplest way to
                # influence generate_architecture without reshaping it is to
                # temporarily extend the spec.
                concerns_hint = (
                    "\n\nIMPORTANT FEEDBACK from architecture review — your "
                    "previous architecture had these gaps. Address ALL of them "
                    "in the revised architecture:\n- "
                    + "\n- ".join(verdict["concerns"])
                )
                orig_desc = project.description
                project.description = orig_desc + concerns_hint
                try:
                    new_arch = generate_architecture(project, provider_info)
                    project.api_calls += 1
                    if new_arch and new_arch.get("modules"):
                        project.architecture = new_arch
                        logger.info(
                            f"🏗️ [{project.project_id}] re-architect produced "
                            f"{len(new_arch.get('modules', []))} modules"
                        )
                    else:
                        logger.warning(
                            f"🏗️ [{project.project_id}] re-architect failed — "
                            f"proceeding with original architecture"
                        )
                finally:
                    project.description = orig_desc
                # Refresh `arch` so the rest of the stage uses the new one
                arch = project.architecture
        except Exception as e:
            logger.warning(
                f"🏗️ [{project.project_id}] architecture-judge raised: {e}",
                exc_info=True,
            )

        # Extract service definitions from architecture (for Docker environments)
        arch_services = arch.get("services", [])
        if arch_services:
            from .models import ServiceDefinition as SvcDef
            for s in arch_services:
                if isinstance(s, dict):
                    project.services.append(SvcDef(
                        name=s.get("name", ""),
                        image=s.get("image", ""),
                        ports=s.get("ports", []),
                        env_vars=s.get("env_vars", {}),
                        health_check=s.get("health_check", ""),
                    ))

        # Create ForgeModule objects from architecture
        modules_spec = arch.get("modules", [])
        for m in modules_spec:
            module = ForgeModule(
                filename=m.get("filename", ""),
                language=m.get("language", project.language),
                description=m.get("description", ""),
                interfaces=m.get("interfaces", ""),
                dependencies=[],  # resolved from architecture
            )
            # Map string dependencies to module IDs
            for dep_name in m.get("dependencies", []):
                for other in project.modules:
                    if other.filename == dep_name:
                        module.dependencies.append(other.module_id)
            project.modules.append(module)

        project.stage_timings["architect"] = time.time() - stage_start
        self._save_project(project)
        logger.info(f"🏗️ [{project.project_id}] Architecture: {len(project.modules)} modules")

    def _stage_generate(self, project: ForgeProject, provider_info: dict):
        """Stage 3: Generate code for each module — Claude-Code-style agent loop
        with real execution feedback.

        For each module in topological dependency order:
          1. Call the LLM (with actual source of upstream dependencies in context)
          2. Write to disk in the project's work_dir
          3. Run real CPython `py_compile` on the file
          4. Try `python -c "import <module>"` from the work_dir
          5. On any failure → feed the actual stderr back to the LLM and retry
          6. Up to `max_iters` per module

        This replaces the original "one LLM call per module, syntax-check only"
        flow that produced packages with hallucinated imports.
        """
        stage_start = time.time()
        project.status = ForgeStatus.GENERATING.value
        project.current_stage = "generate"
        self._save_project(project)

        logger.info(f"⚡ [{project.project_id}] Stage 3: Generating code "
                    f"(agent-loop mode)...")

        # Resolve the work_dir where the agent loop will write + run files.
        work_dir = Path(self._project_workdir(project))
        work_dir.mkdir(parents=True, exist_ok=True)

        # ── Runtime detection ──
        # Check Node once per build. If a project declares JS/TS modules but
        # the host lacks Node, those modules will be marked SKIPPED (written
        # for inspection, not verified). Python-only builds are unaffected.
        has_node_modules = any(_runtimes.is_node_module(m.filename) for m in project.modules)
        if has_node_modules:
            ok, info = _runtimes.detect_node()
            if not ok:
                logger.warning(
                    f"⚡ [{project.project_id}] Node not found ({info}) — "
                    f"JS/TS modules will be marked SKIPPED. Install Node ≥18 "
                    f"to enable fullstack builds."
                )
            else:
                logger.info(
                    f"⚡ [{project.project_id}] Node detected: {info}. "
                    f"npm_install on first JS module."
                )

        # Sort modules by dependency order (generate dependencies first)
        ordered = self._topo_sort_modules(project.modules)

        api_counter = {"n": 0}
        for module in ordered:
            if project.status == ForgeStatus.FAILED.value:
                break

            module.status = ModuleStatus.GENERATING.value
            self._save_project(project)

            logger.info(f"  ⚡ Generating: {module.filename}")
            ok = generate_module_iteratively(
                module, project, provider_info,
                work_dir=work_dir,
                max_iters=5,
                api_call_counter=api_counter,
            )
            project.api_calls += api_counter["n"]
            api_counter["n"] = 0

            if not ok:
                project.error_log.append(
                    f"Failed after agent-loop retries: {module.filename} "
                    f"(last error: {(module.test_output or '')[:200]})"
                )
                self._save_project(project)
                continue

            # ── Per-module tests in the same agent-loop pattern ──
            if (module.language or "").lower() == "python":
                generate_tests_iteratively(
                    module, project, provider_info,
                    work_dir=work_dir,
                    max_iters=3,
                    api_call_counter=api_counter,
                )
                project.api_calls += api_counter["n"]
                api_counter["n"] = 0

            self._save_project(project)

        # ── Cross-module repair pass ──
        # Every module passed its own import probe at generation time, but
        # later-generated peers may have broken contracts the earlier ones
        # depended on. Re-import every module from a clean process and run
        # one focused agent-loop iteration on anything that now fails.
        try:
            repair_counter = {"n": 0}
            repaired = repair_cross_module_breakage(
                project, provider_info,
                work_dir=work_dir,
                max_iters=3,
                api_call_counter=repair_counter,
            )
            project.api_calls += repair_counter["n"]
            if repaired:
                project.error_log.append(
                    f"cross-module repair fixed {repaired} module(s) broken "
                    f"by later peers"
                )
        except Exception as e:
            logger.warning(
                f"⚡ [{project.project_id}] cross-module repair raised: {e}",
                exc_info=True,
            )

        project.stage_timings["generate"] = time.time() - stage_start
        generated = sum(1 for m in project.modules
                        if m.status == ModuleStatus.GENERATED.value)
        total = len(project.modules)
        logger.info(
            f"⚡ [{project.project_id}] Generated {generated}/{total} modules "
            f"(agent-loop). api_calls: {project.api_calls}"
        )
        # If less than half of modules generated, mark project failed early
        if total > 0 and generated < max(1, total // 2):
            project.status = ForgeStatus.FAILED.value
            project.error_log.append(
                f"Agent-loop generation produced only {generated}/{total} working modules"
            )
            self._save_project(project)

    def _project_workdir(self, project: ForgeProject) -> str:
        """Where the agent loop writes + runs files for this project.

        Uses a per-project staging dir under the forge tree so the package
        builds incrementally and `_stage_package` can pick it up.
        """
        return str(FORGE_BASE / project.project_id / "work")

    def _stage_test(self, project: ForgeProject, provider_info: dict):
        """Stage 4: Run tests and fix-iterate on failures."""
        stage_start = time.time()
        project.status = ForgeStatus.TESTING.value
        project.current_stage = "test"
        self._save_project(project)

        logger.info(f"🧪 [{project.project_id}] Stage 4: Testing...")

        passed, failed, output = run_all_tests(project)
        logger.info(f"🧪 [{project.project_id}] Tests: {passed} passed, {failed} failed")

        # Fix-iterate on failures
        if failed > 0:
            for retry in range(project.max_retries):
                failed_modules = [m for m in project.modules
                                  if m.status == ModuleStatus.FAILED.value
                                  and m.retries < m.max_retries]
                if not failed_modules:
                    break

                project.status = ForgeStatus.FIX_ITERATING.value
                self._save_project(project)
                logger.info(f"🔧 [{project.project_id}] Fix iteration {retry + 1}: "
                            f"{len(failed_modules)} modules to fix")

                for module in failed_modules:
                    module.retries += 1
                    fixed_code = fix_module_code(
                        module, project, module.test_output, provider_info
                    )
                    project.api_calls += 1

                    if fixed_code:
                        module.implementation = fixed_code
                        module.status = ModuleStatus.GENERATED.value
                    self._save_project(project)

                # Re-run all tests
                passed, failed, output = run_all_tests(project)
                logger.info(f"🧪 [{project.project_id}] Re-test: {passed} passed, {failed} failed")

                if failed == 0:
                    break

        project.stage_timings["test"] = time.time() - stage_start
        self._save_project(project)

    def _stage_validate(self, project: ForgeProject):
        """Stage 5: Final validation — syntax, security, quality scoring,
        plus an end-to-end pytest run AND critic-gate adversarial review
        against the assembled package on disk."""
        stage_start = time.time()
        project.status = ForgeStatus.VALIDATING.value
        project.current_stage = "validate"
        self._save_project(project)

        logger.info(f"✅ [{project.project_id}] Stage 5: Validating...")

        for module in project.modules:
            passed, issues = validate_module(module)
            if not passed:
                for issue in issues:
                    project.error_log.append(f"{module.filename}: {issue}")

        # Build quality report
        project.quality = build_quality_report(project.modules)

        # ── Real end-to-end pytest from the work_dir ──
        work_dir = Path(self._project_workdir(project))
        if (work_dir / "tests").is_dir():
            try:
                import subprocess as _sp
                r = _sp.run(
                    [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=line"],
                    cwd=str(work_dir), capture_output=True, text=True, timeout=180,
                )
                ok = r.returncode == 0
                summary_line = ""
                for ln in (r.stdout or "").splitlines():
                    if "passed" in ln or "failed" in ln or "error" in ln:
                        summary_line = ln.strip()
                logger.info(
                    f"✅ [{project.project_id}] End-to-end pytest: "
                    f"{'PASS' if ok else 'FAIL'} — {summary_line[:140]}"
                )
                if not ok:
                    tail = (r.stdout + "\n" + r.stderr)[-1500:]
                    project.error_log.append(
                        f"end-to-end pytest failed: {summary_line[:120]}\n{tail}"
                    )
                project.test_results = {
                    "ok": ok,
                    "summary": summary_line,
                    "stdout_tail": (r.stdout or "")[-2000:],
                }
            except Exception as e:
                logger.warning(
                    f"✅ [{project.project_id}] End-to-end pytest skipped: {e}"
                )

        # ── Critic gate adversarial review ──
        # The package on disk is the artifact. Route it through SD-003 (code
        # reviewer) + OL-010 (universal QC) for an external sign-off before
        # we mark the project complete. Falls through gracefully if the
        # critic gate isn't available or no critic agents are reachable.
        try:
            from repryntt.agents.critic_gate import critic_gate as _critic_gate
            _daemon = getattr(self, "_daemon_ref", None)
            if _daemon is not None and (work_dir / "setup.py").exists():
                # Synthesize a typed-task wrapper the gate understands
                synth_task = {
                    "id": project.project_id,
                    "title": f"Forge build: {project.name}",
                    "expected_artifact_type": "code",
                    "expected_location": str(work_dir / "setup.py"),
                    "downstream_consumer": "developer",
                    "success_criterion": (
                        f"package imports cleanly, pytest passes, package "
                        f"described by {project.description[:80] if project.description else 'spec'}"
                    ),
                    "task_type": "forge_validate",
                    "started_at": project.created_at,
                    "completed_at": time.time(),
                }
                doubt = (
                    f"This package was built by an LLM agent loop ({project.api_calls} "
                    f"API calls). It compiled and (per stage_test) imports cleanly, "
                    f"but cross-module integration may still hide bugs, the README "
                    f"may overstate completeness, and tests are AI-authored so they "
                    f"may not catch the same bug the LLM produced. Quality score is "
                    f"{(project.quality.overall_score if project.quality else '?')}/100."
                )
                verdict = _critic_gate(_daemon, str(work_dir / "setup.py"), synth_task, doubt, round_n=1)
                project.critic_verdict = {
                    "pass": verdict.get("pass"),
                    "concerns": verdict.get("concerns", [])[:6],
                    "specialist": verdict.get("specialist"),
                    "universal": verdict.get("universal"),
                }
                if not verdict.get("pass"):
                    project.error_log.append(
                        f"critic_gate blocked: {(verdict.get('concerns') or ['(no concerns recorded)'])[0][:200]}"
                    )
                    logger.warning(
                        f"✅ [{project.project_id}] Critic gate BLOCK at validate: "
                        f"{len(verdict.get('concerns', []))} concerns"
                    )
                else:
                    logger.info(f"✅ [{project.project_id}] Critic gate PASS")
            else:
                logger.info(
                    f"✅ [{project.project_id}] Critic gate skipped "
                    f"(daemon_ref={_daemon is not None}, setup.py={'exists' if (work_dir / 'setup.py').exists() else 'missing'})"
                )
        except Exception as e:
            logger.warning(f"✅ [{project.project_id}] Critic gate raised: {e}", exc_info=True)

        project.stage_timings["validate"] = time.time() - stage_start
        self._save_project(project)

        logger.info(f"✅ [{project.project_id}] Quality score: "
                     f"{project.quality.overall_score}/100")

    def _stage_package(self, project: ForgeProject, provider_info: dict):
        """Stage 6: Package deliverable and register artifact."""
        stage_start = time.time()
        project.status = ForgeStatus.PACKAGING.value
        project.current_stage = "package"
        self._save_project(project)

        logger.info(f"📦 [{project.project_id}] Stage 6: Packaging...")

        # Generate README
        readme = generate_readme(project, provider_info)
        project.api_calls += 1
        if readme:
            readme_module = ForgeModule(
                filename="README.md",
                language="markdown",
                description="Project documentation",
                implementation=readme,
                status=ModuleStatus.PASSED.value,
            )
            project.modules.append(readme_module)

        # Generate CI/CD config
        ci_config = generate_ci_config(project, provider_info)
        project.api_calls += 1
        if ci_config:
            ci_module = ForgeModule(
                filename=".github/workflows/ci.yml",
                language="yaml",
                description="GitHub Actions CI/CD workflow",
                implementation=ci_config,
                status=ModuleStatus.PASSED.value,
            )
            project.modules.append(ci_module)

        # Generate docker-compose.yml for projects with services
        if project.services:
            from .environments import generate_compose_yaml, get_service_env_vars
            compose_yaml = generate_compose_yaml(project.services, project.name)
            compose_module = ForgeModule(
                filename="docker-compose.yml",
                language="yaml",
                description="Docker Compose service orchestration",
                implementation=compose_yaml,
                status=ModuleStatus.PASSED.value,
            )
            project.modules.append(compose_module)

        # Package
        pkg_path = package_project(project, FORGE_BASE)
        project.package_path = pkg_path

        # Register in content store
        register_artifact(project, pkg_path)

        # ── Publish to andrewshub (GitHub) ──
        self._publish_to_hub(project, pkg_path)

        project.status = ForgeStatus.COMPLETED.value
        project.completed_at = time.time()
        project.stage_timings["package"] = time.time() - stage_start
        self._save_project(project)

        total_time = project.completed_at - project.created_at
        logger.info(
            f"🎉 [{project.project_id}] COMPLETE: {project.name} | "
            f"Score: {project.quality.overall_score if project.quality else '?'}/100 | "
            f"Modules: {len(project.modules)} | "
            f"API calls: {project.api_calls} | "
            f"Time: {total_time:.0f}s"
        )

    # ──────────────────────────────────────────────────────────────
    # PUBLISH TO ANDREWSHUB
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _publish_to_hub(project: ForgeProject, pkg_path: str):
        """Publish completed forge project to Andrew's Hub (GitHub) repo."""
        try:
            from repryntt.tools.git_publish import hub_publish
            import json as _json

            pkg = Path(pkg_path)
            if not pkg.exists():
                return

            # Publish each source file under codeforge/<project-name>/
            hub_prefix = f"codeforge/{project.name}"
            published = 0
            for f in sorted(pkg.rglob("*")):
                if not f.is_file():
                    continue
                # Skip metadata files
                if f.name in (".forge_meta.json", "QUALITY_REPORT.json"):
                    continue
                rel = f.relative_to(pkg)
                hub_path = f"{hub_prefix}/{rel}"
                content = f.read_text(encoding="utf-8", errors="replace")
                result = hub_publish(
                    filepath=hub_path,
                    content=content,
                    commit_message=f"CodeForge: {project.name} — {rel}",
                )
                result_data = _json.loads(result)
                if result_data.get("success"):
                    published += 1

            if published:
                logger.info(
                    f"📤 [{project.project_id}] Published {published} files "
                    f"to andrewshub/{hub_prefix}"
                )
        except ImportError:
            logger.debug("git_publish not available — skipping hub publish")
        except Exception as e:
            logger.warning(f"Failed to publish to hub: {e}")

    # ──────────────────────────────────────────────────────────────
    # HELPERS
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _topo_sort_modules(modules: List[ForgeModule]) -> List[ForgeModule]:
        """Topological sort — generate dependencies before dependents."""
        id_to_module = {m.module_id: m for m in modules}
        visited = set()
        order = []

        def visit(mod_id):
            if mod_id in visited:
                return
            visited.add(mod_id)
            m = id_to_module.get(mod_id)
            if m:
                for dep_id in m.dependencies:
                    visit(dep_id)
                order.append(m)

        for m in modules:
            visit(m.module_id)
        return order


# ── Singleton ──
_forge_instance: Optional[CodeForge] = None
_forge_lock = threading.Lock()


def get_forge() -> CodeForge:
    """Get or create the global CodeForge instance."""
    global _forge_instance
    if _forge_instance is None:
        with _forge_lock:
            if _forge_instance is None:
                _forge_instance = CodeForge()
    return _forge_instance
