"""
CodeForge Swarm — Decentralized multi-node code generation.

Enables multiple REPRYNTT nodes around the world to collaborate on a single
forge project. The coordinator splits modules across qualified nodes, each
node generates code for its assigned modules, and results are merged back.

Key design:
- Uses the existing P2P network (SAIGENode) for communication
- Nodes must pass a benchmark before they can contribute (prevents bad models)
- Each module is assigned to exactly one node (no redundant work)
- The coordinator owns the project and merges all results
- If a node fails or times out, the module is reassigned to another node
"""

import json
import time
import logging
import asyncio
import threading
from typing import Dict, List, Optional, Any
from pathlib import Path

from .models import (
    ForgeProject, ForgeModule, ModuleStatus,
    SwarmTask, SwarmNodeRole, BenchmarkResult,
)
from .benchmark import (
    run_benchmark, get_cached_benchmark, save_benchmark,
    BENCHMARK_TASKS,
)
from .generator import _load_ai_config, _resolve_provider, _call_llm

logger = logging.getLogger("codeforge.swarm")

# Timeout for waiting on a swarm node's response
SWARM_TASK_TIMEOUT = 600  # 10 minutes per module
# Maximum nodes that can join a forge swarm
MAX_SWARM_NODES = 20
# Message types for forge-specific P2P communication
MSG_FORGE_RECRUIT = 0x30
MSG_FORGE_JOIN = 0x31
MSG_FORGE_TASK_ASSIGN = 0x32
MSG_FORGE_TASK_RESULT = 0x33
MSG_FORGE_BENCHMARK_REQUEST = 0x34
MSG_FORGE_BENCHMARK_RESULT = 0x35
MSG_FORGE_STATUS = 0x36


class ForgeSwarm:
    """
    Manages distributed code generation across REPRYNTT P2P network nodes.

    The swarm has two modes:
    1. COORDINATOR: Owns the project, splits work, merges results
    2. WORKER: Receives module assignments, generates code, returns results
    """

    def __init__(self):
        self._node = None  # SAIGENode reference (set when P2P is available)
        self._active_projects: Dict[str, ForgeProject] = {}
        self._qualified_nodes: Dict[str, BenchmarkResult] = {}  # node_id → benchmark
        self._pending_tasks: Dict[str, SwarmTask] = {}  # task_id → task
        self._lock = threading.Lock()

    def set_p2p_node(self, node):
        """Connect to the P2P network node."""
        self._node = node

    @property
    def is_connected(self) -> bool:
        return self._node is not None

    # ──────────────────────────────────────────────────────────────
    # COORDINATOR METHODS — called by the node that starts the forge
    # ──────────────────────────────────────────────────────────────

    async def recruit_nodes(self, project: ForgeProject) -> List[str]:
        """
        Broadcast a forge recruitment message to the P2P network.
        Qualified nodes will respond with their benchmark scores.
        Returns list of node_ids that joined.
        """
        if not self._node:
            logger.warning("No P2P node connected — cannot recruit swarm")
            return []

        # Broadcast recruitment
        payload = {
            "project_id": project.project_id,
            "project_name": project.name,
            "language": project.language,
            "framework": project.framework,
            "module_count": len(project.modules),
            "min_benchmark": project.min_benchmark_score,
            "coordinator": self._node.node_id,
        }

        try:
            await self._node._broadcast(MSG_FORGE_RECRUIT, payload)
            logger.info(f"📡 Forge recruitment broadcast for {project.name}")
        except Exception as e:
            logger.error(f"Failed to broadcast recruitment: {e}")
            return []

        # Wait for responses (up to 30 seconds)
        deadline = time.time() + 30
        while time.time() < deadline:
            with self._lock:
                qualified = [
                    nid for nid, bench in self._qualified_nodes.items()
                    if bench.score >= project.min_benchmark_score
                    and bench.is_valid
                ]
            if len(qualified) >= min(len(project.modules), MAX_SWARM_NODES):
                break
            await asyncio.sleep(2)

        logger.info(f"📡 {len(qualified)} qualified nodes joined the forge swarm")
        return qualified

    def assign_modules(self, project: ForgeProject,
                       node_ids: List[str]) -> List[SwarmTask]:
        """
        Distribute modules across qualified nodes.
        Uses round-robin with preference for nodes with higher benchmark scores.
        """
        if not node_ids:
            return []

        # Sort nodes by benchmark score (highest first)
        sorted_nodes = sorted(
            node_ids,
            key=lambda nid: self._qualified_nodes.get(nid, BenchmarkResult()).score,
            reverse=True
        )

        tasks = []
        for i, module in enumerate(project.modules):
            # Round-robin assignment
            node_id = sorted_nodes[i % len(sorted_nodes)]
            bench = self._qualified_nodes.get(node_id, BenchmarkResult())

            task = SwarmTask(
                project_id=project.project_id,
                module_id=module.module_id,
                role=SwarmNodeRole.GENERATOR.value,
                node_id=node_id,
                benchmark_score=bench.score,
            )
            tasks.append(task)
            module.assigned_node = node_id

            with self._lock:
                self._pending_tasks[task.task_id] = task

        project.swarm_tasks = tasks
        logger.info(f"📋 Assigned {len(tasks)} modules across {len(set(t.node_id for t in tasks))} nodes")
        return tasks

    async def dispatch_tasks(self, project: ForgeProject):
        """Send task assignments to worker nodes."""
        if not self._node:
            return

        for task in project.swarm_tasks:
            module = next(
                (m for m in project.modules if m.module_id == task.module_id),
                None
            )
            if not module:
                continue

            # Build context
            other_interfaces = []
            for m in project.modules:
                if m.module_id != module.module_id and m.interfaces:
                    other_interfaces.append(f"# {m.filename}\n{m.interfaces}")

            payload = {
                "task_id": task.task_id,
                "project_id": project.project_id,
                "project_name": project.name,
                "language": project.language,
                "framework": project.framework,
                "module": {
                    "module_id": module.module_id,
                    "filename": module.filename,
                    "description": module.description,
                    "interfaces": module.interfaces,
                    "language": module.language,
                },
                "context": "\n\n".join(other_interfaces),
                "dependencies": project.spec.get("dependencies", []),
            }

            # Send to specific node
            for peer in self._node.peers.values():
                if peer.node_id == task.node_id and peer.websocket:
                    try:
                        await self._node._send_msg(
                            peer.websocket, MSG_FORGE_TASK_ASSIGN, payload
                        )
                    except Exception as e:
                        logger.error(f"Failed to dispatch task to {task.node_id}: {e}")

    async def collect_results(self, project: ForgeProject,
                              timeout: float = None) -> int:
        """
        Wait for all swarm task results to come back.
        Returns number of successful results collected.
        """
        if timeout is None:
            timeout = SWARM_TASK_TIMEOUT * len(project.swarm_tasks)

        deadline = time.time() + timeout
        collected = 0

        while time.time() < deadline:
            all_done = True
            for task in project.swarm_tasks:
                if task.status in ("completed", "failed"):
                    continue
                all_done = False

                # Check if result has arrived
                with self._lock:
                    current = self._pending_tasks.get(task.task_id)
                if current and current.status == "completed":
                    # Apply result to module
                    module = next(
                        (m for m in project.modules if m.module_id == task.module_id),
                        None
                    )
                    if module and current.result:
                        module.implementation = current.result
                        module.status = ModuleStatus.GENERATED.value
                        collected += 1
                    task.status = "completed"
                    task.completed_at = time.time()
                elif current and current.status == "failed":
                    task.status = "failed"

            if all_done:
                break
            await asyncio.sleep(3)

        # Handle timed-out tasks
        for task in project.swarm_tasks:
            if task.status not in ("completed", "failed"):
                task.status = "failed"
                logger.warning(f"⏰ Task {task.task_id} timed out (node {task.node_id})")

        return collected

    # ──────────────────────────────────────────────────────────────
    # WORKER METHODS — called when THIS node receives a forge task
    # ──────────────────────────────────────────────────────────────

    def handle_recruitment(self, payload: dict) -> Optional[dict]:
        """
        Handle an incoming forge recruitment message.
        Check if we meet the benchmark, respond if qualified.
        """
        min_score = payload.get("min_benchmark", 60.0)
        project_language = payload.get("language", "python")

        # Check cached benchmark
        my_node_id = self._node.node_id if self._node else "local"
        cached = get_cached_benchmark(my_node_id)

        if cached and cached.is_valid and cached.score >= min_score:
            return {
                "node_id": my_node_id,
                "benchmark_score": cached.score,
                "language_scores": cached.language_scores,
                "model_name": cached.model_name,
            }

        # Need to run benchmark
        logger.info(f"🏁 Running benchmark for forge recruitment...")
        config = _load_ai_config()
        provider_info = _resolve_provider(config)

        def call_fn(prompt: str) -> Optional[str]:
            messages = [{"role": "user", "content": prompt}]
            return _call_llm(messages, provider_info, max_tokens=2000,
                             temperature=0.2)

        result = run_benchmark(
            call_fn,
            node_id=my_node_id,
            model_name=provider_info.get("model", "unknown"),
            provider=provider_info.get("provider", "unknown"),
        )
        save_benchmark(result)

        if result.score >= min_score:
            return {
                "node_id": my_node_id,
                "benchmark_score": result.score,
                "language_scores": result.language_scores,
                "model_name": result.model_name,
            }

        logger.info(f"📉 Benchmark score {result.score} below minimum {min_score}")
        return None  # Not qualified

    def handle_task_assignment(self, payload: dict):
        """
        Handle an incoming task assignment — generate code for a module.
        Runs in a background thread to not block the P2P event loop.
        """
        task_id = payload.get("task_id")
        module_info = payload.get("module", {})

        t = threading.Thread(
            target=self._execute_task,
            args=(task_id, payload),
            daemon=True,
            name=f"forge-worker-{task_id}",
        )
        t.start()

    def _execute_task(self, task_id: str, payload: dict):
        """Execute a forge task — generate code for assigned module."""
        try:
            config = _load_ai_config()
            provider_info = _resolve_provider(config)
            module_info = payload.get("module", {})
            context = payload.get("context", "")
            language = module_info.get("language", "python")

            messages = [
                {"role": "system", "content": (
                    f"You are an expert {language} developer in a distributed team. "
                    f"Write production-quality code for the specified module. "
                    "Write COMPLETE, working code — no stubs or TODOs. "
                    "Include proper error handling. "
                    "Do NOT use eval(), exec(), or unsafe patterns. "
                    f"Reply with ONLY the {language} code in a code block."
                )},
                {"role": "user", "content": (
                    f"Project: {payload.get('project_name', '')}\n"
                    f"Module: {module_info.get('filename', '')}\n"
                    f"Description: {module_info.get('description', '')}\n"
                    f"Interfaces:\n{module_info.get('interfaces', '')}\n\n"
                    f"Other module interfaces:\n{context}\n\n"
                    f"Dependencies: {', '.join(payload.get('dependencies', []))}\n"
                    f"Write the complete implementation."
                )},
            ]

            response = _call_llm(messages, provider_info, max_tokens=6000,
                                 temperature=0.3)

            if response:
                # Extract code from response
                import re
                match = re.search(rf"```{language}\s*\n(.*?)```",
                                  response, re.DOTALL)
                if match:
                    code = match.group(1).strip()
                else:
                    match = re.search(r"```\w*\s*\n(.*?)```",
                                      response, re.DOTALL)
                    code = match.group(1).strip() if match else response.strip()

                # Send result back to coordinator
                self._send_task_result(task_id, payload.get("project_id", ""),
                                       module_info.get("module_id", ""),
                                       code, "completed")
            else:
                self._send_task_result(task_id, payload.get("project_id", ""),
                                       module_info.get("module_id", ""),
                                       "", "failed")

        except Exception as e:
            logger.error(f"Task {task_id} execution failed: {e}")
            self._send_task_result(task_id, payload.get("project_id", ""),
                                   payload.get("module", {}).get("module_id", ""),
                                   "", "failed")

    def _send_task_result(self, task_id: str, project_id: str,
                          module_id: str, code: str, status: str):
        """Send a task result back to the coordinator via P2P."""
        if not self._node:
            # Local mode — update pending tasks directly
            with self._lock:
                task = self._pending_tasks.get(task_id)
                if task:
                    task.result = code
                    task.status = status
                    task.completed_at = time.time()
            return

        payload = {
            "task_id": task_id,
            "project_id": project_id,
            "module_id": module_id,
            "code": code,
            "status": status,
            "node_id": self._node.node_id,
        }

        # Broadcast result (coordinator will pick it up)
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(
                    self._node._broadcast(MSG_FORGE_TASK_RESULT, payload)
                )
            else:
                loop.run_until_complete(
                    self._node._broadcast(MSG_FORGE_TASK_RESULT, payload)
                )
        except Exception as e:
            logger.error(f"Failed to send task result: {e}")

    def handle_task_result(self, payload: dict):
        """Handle an incoming task result from a worker node."""
        task_id = payload.get("task_id")
        with self._lock:
            task = self._pending_tasks.get(task_id)
            if task:
                task.result = payload.get("code", "")
                task.status = payload.get("status", "completed")
                task.completed_at = time.time()
                logger.info(f"📬 Received result for task {task_id} "
                            f"from {payload.get('node_id', '?')}")

    def handle_benchmark_result(self, payload: dict):
        """Handle an incoming benchmark result from a node wanting to join."""
        node_id = payload.get("node_id", "")
        score = payload.get("benchmark_score", 0)

        if node_id and score > 0:
            bench = BenchmarkResult(
                node_id=node_id,
                score=score,
                model_name=payload.get("model_name", ""),
                language_scores=payload.get("language_scores", {}),
            )
            with self._lock:
                self._qualified_nodes[node_id] = bench
            logger.info(f"📊 Node {node_id} benchmark: {score}/100")

    # ──────────────────────────────────────────────────────────────
    # STATUS
    # ──────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Get swarm status summary."""
        with self._lock:
            return {
                "connected": self.is_connected,
                "node_id": self._node.node_id if self._node else None,
                "qualified_nodes": len(self._qualified_nodes),
                "pending_tasks": len([t for t in self._pending_tasks.values()
                                      if t.status == "assigned"]),
                "completed_tasks": len([t for t in self._pending_tasks.values()
                                        if t.status == "completed"]),
                "failed_tasks": len([t for t in self._pending_tasks.values()
                                     if t.status == "failed"]),
                "nodes": {
                    nid: {
                        "score": b.score,
                        "model": b.model_name,
                        "valid": b.is_valid,
                    }
                    for nid, b in self._qualified_nodes.items()
                },
            }


# ── Singleton ──
_swarm_instance: Optional[ForgeSwarm] = None
_swarm_lock = threading.Lock()


def get_swarm() -> ForgeSwarm:
    """Get or create the global ForgeSwarm instance."""
    global _swarm_instance
    if _swarm_instance is None:
        with _swarm_lock:
            if _swarm_instance is None:
                _swarm_instance = ForgeSwarm()
    return _swarm_instance
