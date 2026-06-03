"""
repryntt.economy.workload_marketplace — Workload Marketplace

External users submit AI workloads, node operators earn CR by processing them.
Routes through the P2P network: WorkloadRouter picks the best node, workload
submitted to blockchain via qnode2 TCP, miner claims and processes, result
returned to submitter. Falls back to local LLM if solo node.

Workload types:
  inference  — single prompt → response
  batch      — multiple prompts → multiple responses
  embedding  — text list → vector embeddings
  analysis   — deep chain-of-thought analysis

Flow (networked):
  1. User submits workload via /ext-api/workloads/submit, CR reserved
  2. WorkloadRouter picks best available node from ResourceRegistry
  3. Workload submitted to blockchain via qnode2 TCP (submit_workload)
  4. Miner on network claims workload, runs LLM, submits PoP result
  5. Marketplace polls contract for result, returns to user
  6. Falls back to local LLM if no peers or routing fails

Flow (solo node / local fallback):
  1. Same submission, CR reserved
  2. No remote peers → process locally via route_ai_call()
  3. PoP proof hash generated, operator earns CR
  4. Result returned to user
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import struct
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Job statuses
PENDING = "pending"
PROCESSING = "processing"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

_STORE_DIR = os.path.join(str(Path.home()), ".repryntt", "data", "workloads")


class WorkloadMarketplace:
    """Singleton workload marketplace — submit, queue, process, settle."""

    _instance: Optional["WorkloadMarketplace"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "WorkloadMarketplace":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._initialized = False
                    cls._instance = inst
        return cls._instance

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(
        self,
        brain_system=None,
        economy_manager=None,
        qnode=None,
        resource_registry=None,
        workload_router=None,
    ) -> None:
        if self._initialized:
            return
        self._initialized = True

        self.brain_system = brain_system
        self.economy_manager = economy_manager
        self.qnode = qnode  # For PoP proof submission + TCP access
        self.resource_registry = resource_registry  # P2P node discovery
        self.workload_router = workload_router      # P2P workload routing

        # Blockchain TCP config (for submitting workloads to the P2P network)
        self._node_host = os.environ.get("REPRYNTT_NODE_HOST", "127.0.0.1")
        self._node_port = int(os.environ.get("REPRYNTT_NODE_PORT", "5001"))

        os.makedirs(_STORE_DIR, exist_ok=True)
        from repryntt.web.ext_api_store import PersistentDict

        self.jobs: PersistentDict = PersistentDict(os.path.join(_STORE_DIR, "jobs.json"))
        self.node_config: PersistentDict = PersistentDict(os.path.join(_STORE_DIR, "node_config.json"))
        self.stats: PersistentDict = PersistentDict(os.path.join(_STORE_DIR, "stats.json"))

        # Seed defaults
        if "accepting_workloads" not in self.node_config:
            self.node_config.update({
                "accepting_workloads": True,
                "pricing": {
                    "inference_per_1k_tokens": 0.02,
                    "embedding_per_1k_tokens": 0.01,
                    "analysis_per_request": 0.10,
                    "batch_discount": 0.80,
                },
                "max_concurrent_jobs": 2,
                "max_queue_depth": 50,
                "max_tokens_limit": 4096,
            })
            self.node_config.sync()

        if "total_jobs" not in self.stats:
            self.stats.update({
                "total_jobs": 0,
                "completed_jobs": 0,
                "failed_jobs": 0,
                "total_cr_earned": 0.0,
                "total_tokens_processed": 0,
            })
            self.stats.sync()

        # Recover any jobs stuck in PROCESSING from a crash
        for jid, job in list(self.jobs.items()):
            if isinstance(job, dict) and job.get("status") == PROCESSING:
                job["status"] = PENDING
                self.jobs[jid] = job
        self.jobs.sync()

        self._worker_running = False
        self._worker_thread: Optional[threading.Thread] = None
        logger.info(f"🏭 Workload marketplace initialized ({len(self.jobs)} queued jobs)")

    def start_worker(self) -> None:
        if self._worker_running:
            return
        self._worker_running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="workload-worker", daemon=True
        )
        self._worker_thread.start()
        logger.info("🏭 Workload marketplace worker started")

    def stop_worker(self) -> None:
        self._worker_running = False
        if self._worker_thread:
            self._worker_thread.join(timeout=10)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit_job(
        self,
        user_wallet: str,
        workload_type: str,
        payload: Dict[str, Any],
        max_price_cr: float = 0.0,
    ) -> Dict[str, Any]:
        """Submit a workload.  Returns {success, job_id, estimated_cost_cr, ...}."""
        if not self.node_config.get("accepting_workloads", True):
            return {"success": False, "error": "Node is not accepting workloads"}

        pending = sum(
            1 for j in self.jobs.values()
            if isinstance(j, dict) and j.get("status") == PENDING
        )
        cap = self.node_config.get("max_queue_depth", 50)
        if pending >= cap:
            return {"success": False, "error": f"Queue full ({pending}/{cap})"}

        valid_types = ("inference", "batch", "embedding", "analysis")
        if workload_type not in valid_types:
            return {"success": False, "error": f"Unsupported type: {workload_type}. Use: {valid_types}"}

        estimated_cost = self._estimate_cost(workload_type, payload)

        if max_price_cr > 0 and estimated_cost > max_price_cr:
            return {
                "success": False,
                "error": f"Estimated cost {estimated_cost:.4f} CR exceeds your max_price {max_price_cr:.4f} CR",
            }

        if not self.economy_manager:
            return {"success": False, "error": "Economy system not available"}

        balance = self.economy_manager.get_wallet_balance(user_wallet)
        if not balance.get("success"):
            return {"success": False, "error": "Invalid wallet"}

        available = balance.get("balance_credits", 0)
        if available < estimated_cost:
            return {
                "success": False,
                "error": f"Insufficient credits. Need {estimated_cost:.4f} CR, have {available:.4f} CR",
            }

        deduct = self.economy_manager.deduct_credits(
            user_wallet, estimated_cost, f"workload_reserve_{workload_type}"
        )
        if not deduct.get("success"):
            return {"success": False, "error": "Failed to reserve credits"}

        job_id = uuid.uuid4().hex
        job: Dict[str, Any] = {
            "job_id": job_id,
            "user_wallet": user_wallet,
            "workload_type": workload_type,
            "payload": payload,
            "status": PENDING,
            "created_at": datetime.now().isoformat(),
            "started_at": None,
            "completed_at": None,
            "result": None,
            "error": None,
            "reserved_cr": estimated_cost,
            "actual_cost_cr": 0.0,
            "pop_proof_hash": None,
            "tokens_in": 0,
            "tokens_out": 0,
            "processing_time_s": 0.0,
        }

        self.jobs[job_id] = job
        self.jobs.sync()
        self.stats["total_jobs"] = self.stats.get("total_jobs", 0) + 1
        self.stats.sync()

        logger.info(
            f"📥 Workload submitted: {job_id[:12]}... type={workload_type} "
            f"reserved={estimated_cost:.4f} CR"
        )
        return {
            "success": True,
            "job_id": job_id,
            "estimated_cost_cr": estimated_cost,
            "status": PENDING,
            "queue_position": pending + 1,
        }

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        job = self.jobs.get(job_id)
        if not job or not isinstance(job, dict):
            return None
        # Strip payload from completed jobs to keep responses small
        out = dict(job)
        if out.get("status") == COMPLETED and out.get("result"):
            out.pop("payload", None)
        return out

    def list_jobs(
        self,
        user_wallet: str,
        limit: int = 50,
        status_filter: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        for jid, job in self.jobs.items():
            if not isinstance(job, dict):
                continue
            if job.get("user_wallet") != user_wallet:
                continue
            if status_filter and job.get("status") != status_filter:
                continue
            out = dict(job)
            out.pop("payload", None)
            results.append(out)
        results.sort(key=lambda j: j.get("created_at", ""), reverse=True)
        return results[:limit]

    def cancel_job(self, job_id: str, user_wallet: str) -> Dict[str, Any]:
        job = self.jobs.get(job_id)
        if not job or not isinstance(job, dict):
            return {"success": False, "error": "Job not found"}
        if job.get("user_wallet") != user_wallet:
            return {"success": False, "error": "Unauthorized"}
        if job.get("status") != PENDING:
            return {"success": False, "error": f"Cannot cancel job in '{job['status']}' state"}

        reserved = job.get("reserved_cr", 0)
        if reserved > 0 and self.economy_manager:
            self.economy_manager.add_credits(user_wallet, reserved, "workload_cancel_refund")

        job["status"] = CANCELLED
        job["completed_at"] = datetime.now().isoformat()
        self.jobs[job_id] = job
        self.jobs.sync()
        return {"success": True, "refunded_cr": reserved}

    def get_node_config(self) -> Dict[str, Any]:
        return dict(self.node_config)

    def update_node_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "accepting_workloads", "pricing", "max_concurrent_jobs",
            "max_queue_depth", "max_tokens_limit",
        }
        for k, v in updates.items():
            if k in allowed:
                self.node_config[k] = v
        self.node_config.sync()
        return dict(self.node_config)

    def get_stats(self) -> Dict[str, Any]:
        pending = sum(1 for j in self.jobs.values() if isinstance(j, dict) and j.get("status") == PENDING)
        processing = sum(1 for j in self.jobs.values() if isinstance(j, dict) and j.get("status") == PROCESSING)
        return {
            **dict(self.stats),
            "queue_pending": pending,
            "queue_processing": processing,
            "accepting_workloads": self.node_config.get("accepting_workloads", True),
        }

    # ------------------------------------------------------------------
    # Cost estimation
    # ------------------------------------------------------------------

    def _estimate_cost(self, workload_type: str, payload: Dict[str, Any]) -> float:
        pricing = self.node_config.get("pricing", {})
        max_tok = self.node_config.get("max_tokens_limit", 4096)

        if workload_type == "inference":
            prompt_tok = len(str(payload.get("prompt", "") or payload.get("messages", ""))) / 4
            out_tok = min(payload.get("max_tokens", 500), max_tok)
            rate = pricing.get("inference_per_1k_tokens", 0.02)
            return max((prompt_tok + out_tok) / 1000 * rate, 0.001)

        if workload_type == "batch":
            prompts = payload.get("prompts", [])
            out_tok = min(payload.get("max_tokens", 500), max_tok)
            total = sum(len(str(p)) / 4 + out_tok for p in prompts)
            rate = pricing.get("inference_per_1k_tokens", 0.02)
            discount = pricing.get("batch_discount", 0.80)
            return max(total / 1000 * rate * discount, 0.001)

        if workload_type == "embedding":
            texts = payload.get("texts", [])
            total = sum(len(str(t)) / 4 for t in texts)
            rate = pricing.get("embedding_per_1k_tokens", 0.01)
            return max(total / 1000 * rate, 0.001)

        if workload_type == "analysis":
            return pricing.get("analysis_per_request", 0.10)

        return 0.01

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        while self._worker_running:
            try:
                next_job = None
                for jid, job in list(self.jobs.items()):
                    if isinstance(job, dict) and job.get("status") == PENDING:
                        next_job = job
                        break

                if not next_job:
                    time.sleep(1)
                    continue

                processing = sum(
                    1 for j in self.jobs.values()
                    if isinstance(j, dict) and j.get("status") == PROCESSING
                )
                limit = self.node_config.get("max_concurrent_jobs", 2)
                if processing >= limit:
                    time.sleep(2)
                    continue

                self._process_job(next_job)
            except Exception as e:
                logger.error(f"Workload worker error: {e}")
                time.sleep(5)

    def _process_job(self, job: Dict[str, Any]) -> None:
        job_id = job["job_id"]
        wtype = job["workload_type"]
        payload = job["payload"]

        job["status"] = PROCESSING
        job["started_at"] = datetime.now().isoformat()
        self.jobs[job_id] = job
        self.jobs.sync()

        logger.info(f"⚙️  Processing workload {job_id[:12]}... type={wtype}")

        try:
            t0 = time.time()

            # --- Try P2P network first ---
            p2p_result = self._try_p2p_route(job)

            if p2p_result is not None:
                result, tokens_in, tokens_out = p2p_result
                route_method = "p2p"
            else:
                # --- Fallback: local LLM ---
                dispatch = {
                    "inference": self._do_inference,
                    "batch": self._do_batch,
                    "embedding": self._do_embedding,
                    "analysis": self._do_analysis,
                }
                result, tokens_in, tokens_out = dispatch[wtype](payload)
                route_method = "local"

            elapsed = time.time() - t0

            # Actual cost
            pricing = self.node_config.get("pricing", {})
            total_tokens = tokens_in + tokens_out

            if wtype == "analysis":
                actual_cost = pricing.get("analysis_per_request", 0.10)
            elif wtype == "embedding":
                actual_cost = (tokens_in / 1000) * pricing.get("embedding_per_1k_tokens", 0.01)
            else:
                actual_cost = (total_tokens / 1000) * pricing.get("inference_per_1k_tokens", 0.02)
                if wtype == "batch":
                    actual_cost *= pricing.get("batch_discount", 0.80)

            actual_cost = max(actual_cost, 0.001)

            # Settle: refund overpayment
            reserved = job.get("reserved_cr", 0)
            diff = reserved - actual_cost
            if diff > 0.0001 and self.economy_manager:
                self.economy_manager.add_credits(
                    job["user_wallet"], diff, "workload_cost_refund"
                )
            elif diff < -0.0001:
                actual_cost = reserved  # Don't charge more than reserved

            # PoP proof hash
            pop_hash = hashlib.sha256(
                f"{job_id}:{wtype}:{total_tokens}:{elapsed:.3f}:{datetime.now().isoformat()}".encode()
            ).hexdigest()

            # Update job
            job.update({
                "status": COMPLETED,
                "completed_at": datetime.now().isoformat(),
                "result": result,
                "actual_cost_cr": round(actual_cost, 6),
                "pop_proof_hash": pop_hash,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "processing_time_s": round(elapsed, 3),
                "route_method": route_method,
            })
            self.jobs[job_id] = job
            self.jobs.sync()

            # Platform commission (5%) — remainder is operator revenue
            commission = actual_cost * 0.05
            operator_rev = actual_cost - commission

            self.stats["completed_jobs"] = self.stats.get("completed_jobs", 0) + 1
            self.stats["total_cr_earned"] = round(
                self.stats.get("total_cr_earned", 0.0) + operator_rev, 6
            )
            self.stats["total_tokens_processed"] = (
                self.stats.get("total_tokens_processed", 0) + total_tokens
            )
            self.stats.sync()

            logger.info(
                f"✅ Workload {job_id[:12]}... completed via {route_method} in {elapsed:.1f}s — "
                f"{total_tokens} tokens, {actual_cost:.4f} CR, PoP: {pop_hash[:16]}..."
            )

            # Submit PoP to blockchain (non-critical, for local-processed jobs)
            if route_method == "local" and self.qnode:
                try:
                    addr = getattr(self.qnode, "wallet_address", "local_node")
                    self.qnode.submit_ai_workload(
                        machine_address=addr,
                        workload_key=pop_hash[:64],
                        workload_data={"type": wtype, "tokens": total_tokens},
                        workload_type=wtype,
                        purpose=f"external_workload_{wtype}",
                        storage_nodes=[],
                    )
                except Exception as e:
                    logger.warning(f"PoP blockchain submission failed (non-fatal): {e}")

        except Exception as e:
            logger.error(f"❌ Workload {job_id[:12]}... failed: {e}")

            reserved = job.get("reserved_cr", 0)
            if reserved > 0 and self.economy_manager:
                self.economy_manager.add_credits(
                    job["user_wallet"], reserved, "workload_failure_refund"
                )

            job["status"] = FAILED
            job["completed_at"] = datetime.now().isoformat()
            job["error"] = str(e)
            self.jobs[job_id] = job
            self.jobs.sync()

            self.stats["failed_jobs"] = self.stats.get("failed_jobs", 0) + 1
            self.stats.sync()

    # ------------------------------------------------------------------
    # P2P network routing
    # ------------------------------------------------------------------

    def _try_p2p_route(self, job: Dict[str, Any]) -> Optional[tuple]:
        """Try to route workload through the P2P network.

        Returns (result_dict, tokens_in, tokens_out) if a remote node processed
        the workload, or None if we should fall back to local.
        """
        if not self.workload_router or not self.resource_registry:
            return None  # No P2P infrastructure → local fallback

        wtype = job["workload_type"]
        payload = job["payload"]

        # Build a WorkloadSpec for the router
        try:
            from repryntt.economy.workload_router import WorkloadSpec
            spec = WorkloadSpec(
                workload_type=wtype,
                need_gpu=(wtype != "embedding"),
                estimated_duration_s=30.0,
                parallelizable=(wtype == "batch"),
                chunk_count=min(len(payload.get("prompts", [1])), 4) if wtype == "batch" else 1,
            )
            decision = self.workload_router.route(spec)
        except Exception as e:
            logger.debug(f"P2P routing failed: {e}")
            return None

        if not decision.success:
            logger.debug(f"P2P route: no suitable nodes — {decision.reason}")
            return None

        target = decision.primary_node

        # If the best node is us, skip P2P and process locally
        my_node_id = self.resource_registry.node_id if self.resource_registry else ""
        if target.node_id == my_node_id:
            logger.debug("P2P route: best node is self → local processing")
            return None

        # Route to remote node via blockchain TCP protocol
        logger.info(
            f"🌐 P2P routing workload {job['job_id'][:12]}... to node "
            f"{target.node_id[:12]} ({target.host}:{target.port}, "
            f"{target.gpu_name}, rep={target.reputation:.2f})"
        )

        try:
            return self._submit_and_poll_p2p(job, target)
        except Exception as e:
            logger.warning(f"P2P execution failed on {target.node_id[:12]}: {e} — falling back to local")
            return None

    def _submit_and_poll_p2p(
        self, job: Dict[str, Any], target
    ) -> tuple:
        """Submit workload to remote node via qnode2 TCP and poll for result.

        1. submit_workload → blockchain contract adds to pending pool
        2. Target miner claims via get_workload_key → processes → ai_work
        3. We poll get_workload_result on our local contract for the result
        """
        from repryntt.economy.safe_serialize import pack as safe_pack, unpack as safe_unpack

        wtype = job["workload_type"]
        payload = job["payload"]
        job_id = job["job_id"]

        # Build workload key (64-char hex for contract)
        workload_key = hashlib.sha3_256(
            f"{job_id}:{wtype}:{time.time()}".encode()
        ).hexdigest()

        # For AI inference, we embed the payload as workload_data so the miner
        # gets it inline when claiming the workload
        workload_data = {
            "type": wtype,
            "payload": payload,
            "job_id": job_id,
            "max_tokens": payload.get("max_tokens", 500),
        }

        # Get our wallet address for the submission fee
        wallet_addr = "marketplace_submitter"
        if self.qnode and hasattr(self.qnode, "wallet_address"):
            wallet_addr = self.qnode.wallet_address

        # Submit to the blockchain node via TCP
        submit_msg = {
            "type": "submit_workload",
            "machine_address": wallet_addr,
            "workload_key": workload_key,
            "workload_data": workload_data,
            "workload_type": wtype,
            "purpose": f"marketplace_{wtype}_{job_id[:8]}",
            "storage_nodes": [],
        }

        resp = self._tcp_send(target.host, target.port, submit_msg)
        if not resp or not resp.get("success"):
            raise RuntimeError(
                f"P2P submit failed: {resp.get('error', 'no response') if resp else 'connection failed'}"
            )

        logger.info(f"🌐 Workload {job_id[:12]}... submitted to P2P network (key={workload_key[:12]}...)")

        # Poll the contract for the result
        # The miner will claim, process, and submit result via ai_work
        poll_timeout = 180  # 3 minutes max
        poll_interval = 2
        start = time.time()

        while time.time() - start < poll_timeout:
            # Check if the contract has a result for this workload key
            if self.qnode and hasattr(self.qnode, "contract"):
                with self.qnode.lock:
                    if workload_key in self.qnode.contract.workload_results:
                        result_data = self.qnode.contract.workload_results[workload_key]
                        ai_result = result_data.get("result", {})

                        # Parse result — the miner returns the raw AI output
                        if isinstance(ai_result, dict):
                            response_text = ai_result.get("response", ai_result.get("text", str(ai_result)))
                        else:
                            response_text = str(ai_result)

                        tokens_in = len(str(payload)) // 4
                        tokens_out = len(response_text) // 4

                        return (
                            {
                                "response": response_text,
                                "model": "p2p_remote",
                                "miner": result_data.get("miner_address", "unknown"),
                                "p2p_node": target.node_id[:16],
                            },
                            tokens_in,
                            tokens_out,
                        )

                    # Check if workload is still pending (valid key exists)
                    if workload_key not in self.qnode.contract.valid_keys:
                        # Key gone but no result — might have been cleaned up
                        if workload_key not in self.qnode.contract.workload_results:
                            raise RuntimeError("P2P workload lost — not in pending or results")

            time.sleep(poll_interval)

        raise RuntimeError(f"P2P workload timed out after {poll_timeout}s (key={workload_key[:12]}...)")

    def _tcp_send(self, host: str, port: int, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Send a message to a blockchain node via TCP and return the response."""
        try:
            from repryntt.economy.safe_serialize import pack as safe_pack, unpack as safe_unpack

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(15)
                s.connect((host, port))

                data = safe_pack(message)
                s.sendall(struct.pack('!I', len(data)))
                s.sendall(data)

                # Read response length
                length_bytes = s.recv(4)
                if len(length_bytes) < 4:
                    return None
                length = struct.unpack('!I', length_bytes)[0]
                if length > 16 * 1024 * 1024:  # 16MB safety limit
                    return None

                # Read response
                resp_data = b''
                while len(resp_data) < length:
                    chunk = s.recv(min(length - len(resp_data), 65536))
                    if not chunk:
                        return None
                    resp_data += chunk

                return safe_unpack(resp_data)
        except Exception as e:
            logger.debug(f"TCP send to {host}:{port} failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Execution methods — each returns (result_dict, tokens_in, tokens_out)
    # ------------------------------------------------------------------

    def _do_inference(self, payload: Dict[str, Any]):
        from repryntt.routing.provider_router import load_ai_provider_config, route_ai_call

        config = load_ai_provider_config()
        messages = payload.get("messages")
        prompt = payload.get("prompt", "")
        max_tokens = min(
            payload.get("max_tokens", 500),
            self.node_config.get("max_tokens_limit", 4096),
        )
        temperature = payload.get("temperature", 0.7)

        if not messages and prompt:
            messages = [{"role": "user", "content": prompt}]
        if not messages:
            raise ValueError("'prompt' or 'messages' required")

        ai_params = {
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": payload.get("top_p", 0.9),
        }

        resp = route_ai_call(
            config, prompt or messages[-1]["content"], ai_params, messages=messages
        )
        if resp.status_code != 200:
            raise RuntimeError(f"LLM returned HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = data.get("usage", {})
        t_in = usage.get("prompt_tokens", len(str(messages)) // 4)
        t_out = usage.get("completion_tokens", len(content) // 4)

        return {"response": content, "model": data.get("model", "local"), "usage": usage}, t_in, t_out

    def _do_batch(self, payload: Dict[str, Any]):
        prompts = payload.get("prompts", [])
        if not prompts:
            raise ValueError("'prompts' list required for batch workload")

        max_tokens = min(
            payload.get("max_tokens", 500),
            self.node_config.get("max_tokens_limit", 4096),
        )
        temperature = payload.get("temperature", 0.7)

        from repryntt.routing.provider_router import load_ai_provider_config, route_ai_call

        config = load_ai_provider_config()
        results = []
        total_in = 0
        total_out = 0

        for i, p in enumerate(prompts):
            ai_params = {"max_tokens": max_tokens, "temperature": temperature}
            msgs = [{"role": "user", "content": p}]
            try:
                resp = route_ai_call(config, p, ai_params, messages=msgs)
                if resp.status_code != 200:
                    results.append({"index": i, "error": f"LLM HTTP {resp.status_code}"})
                    continue
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                usage = data.get("usage", {})
                t_in = usage.get("prompt_tokens", len(p) // 4)
                t_out = usage.get("completion_tokens", len(content) // 4)
                total_in += t_in
                total_out += t_out
                results.append({"index": i, "response": content, "usage": {"prompt_tokens": t_in, "completion_tokens": t_out}})
            except Exception as e:
                results.append({"index": i, "error": str(e)})

        return {"responses": results, "total_prompts": len(prompts)}, total_in, total_out

    def _do_embedding(self, payload: Dict[str, Any]):
        texts = payload.get("texts", [])
        if not texts:
            raise ValueError("'texts' list required for embedding workload")

        from repryntt.routing.provider_router import load_ai_provider_config

        config = load_ai_provider_config()
        provider = config.get("provider", "local")
        settings = config.get(provider, config.get("local", {}))

        endpoint = settings.get("endpoint", "http://localhost:8080/v1/chat/completions")
        embed_url = endpoint.replace("/v1/chat/completions", "/v1/embeddings")

        import requests as _requests

        resp = _requests.post(
            embed_url,
            json={"input": texts, "model": settings.get("model", "default")},
            headers={"Content-Type": "application/json"},
            timeout=120,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Embedding endpoint HTTP {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        embeddings = [e.get("embedding", []) for e in data.get("data", [])]
        total_tokens = sum(len(t) // 4 for t in texts)
        dims = len(embeddings[0]) if embeddings and embeddings[0] else 0

        return {"embeddings": embeddings, "dimensions": dims, "count": len(embeddings)}, total_tokens, 0

    def _do_analysis(self, payload: Dict[str, Any]):
        query = payload.get("query", "")
        if not query:
            raise ValueError("'query' required for analysis workload")

        if self.brain_system and hasattr(self.brain_system, "_call_ai_service"):
            text = self.brain_system._call_ai_service(
                prompt=f"Perform a detailed analysis: {query}",
                priority=1,
                timeout=180,
            )
            return {"analysis": text}, len(query) // 4, len(text) // 4

        # Fallback to direct inference
        return self._do_inference({"prompt": f"Analyze in detail: {query}", "max_tokens": 2000})


# ------------------------------------------------------------------
# Module-level accessor
# ------------------------------------------------------------------

def get_workload_marketplace() -> WorkloadMarketplace:
    """Return the singleton WorkloadMarketplace instance."""
    return WorkloadMarketplace()
