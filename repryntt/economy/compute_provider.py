"""Compute provider runtime for the Repryntt marketplace.

This is the local provider-side boundary for the production compute market.
It does not assume the blockchain is mandatory for every Repryntt install:
operators can keep this disabled and run the AI system normally.  When enabled,
it produces signed provider announcements and manages a durable local job queue
that later chain/escrow transactions can reference.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from repryntt.economy.compute_config import local_compute_runtime, normalize_compute_share
from repryntt.economy.node_identity import get_local_node_address
from repryntt.paths import get_data_dir


PROVIDER_VERSION = "0.1.0"
DEFAULT_JOB_TIMEOUT_SECONDS = 300
DEFAULT_DISPUTE_WINDOW_SECONDS = 3600


def _now() -> float:
    return round(time.time(), 3)


def _canonical_json(data: dict) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_json(data: dict) -> str:
    return hashlib.sha256(_canonical_json(data)).hexdigest()


def compute_provider_dir() -> Path:
    path = get_data_dir() / "compute"
    path.mkdir(parents=True, exist_ok=True)
    return path


def provider_config_path() -> Path:
    return compute_provider_dir() / "provider.json"


def provider_state_path() -> Path:
    return compute_provider_dir() / "provider_state.json"


def provider_jobs_dir() -> Path:
    path = compute_provider_dir() / "jobs"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class ProviderConfig:
    enabled: bool = False
    chain_enabled: bool = False
    require_chain_for_paid_jobs: bool = False
    provider_id: str = ""
    wallet_address: str = ""
    endpoint: str = "local"
    max_concurrent_jobs: int = 1
    fiat_currency: str = "usd"
    price_per_inference_cents: int = 10
    price_per_second_cents: int = 1
    connected_account_id: str = ""
    supported_task_types: list[str] = field(
        default_factory=lambda: ["health_check", "text_generation", "embedding"]
    )
    execution_mode: str = "local_llm"
    container_runtime: str = "docker"
    allow_job_network: bool = False
    job_timeout_seconds: int = DEFAULT_JOB_TIMEOUT_SECONDS
    dispute_window_seconds: int = DEFAULT_DISPUTE_WINDOW_SECONDS

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "ProviderConfig":
        known = {field.name for field in ProviderConfig.__dataclass_fields__.values()}
        cleaned = {k: v for k, v in dict(data or {}).items() if k in known}
        cfg = ProviderConfig(**cleaned)
        cfg.max_concurrent_jobs = max(1, int(cfg.max_concurrent_jobs))
        cfg.fiat_currency = str(cfg.fiat_currency or "usd").lower()
        cfg.price_per_inference_cents = max(0, int(cfg.price_per_inference_cents))
        cfg.price_per_second_cents = max(0, int(cfg.price_per_second_cents))
        cfg.job_timeout_seconds = max(1, int(cfg.job_timeout_seconds))
        cfg.dispute_window_seconds = max(0, int(cfg.dispute_window_seconds))
        cfg.supported_task_types = [str(t) for t in cfg.supported_task_types if str(t)]
        return cfg


@dataclass
class ComputeJob:
    job_id: str
    buyer_address: str
    task_type: str
    payload: dict
    max_price_cents: int = 0
    escrow_id: str = ""
    model: str = ""
    state: str = "queued"
    provider_id: str = ""
    created_at: float = field(default_factory=_now)
    claimed_at: float = 0.0
    completed_at: float = 0.0
    result_hash: str = ""
    receipt_hash: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "ComputeJob":
        known = {field.name for field in ComputeJob.__dataclass_fields__.values()}
        cleaned = {k: v for k, v in dict(data or {}).items() if k in known}
        return ComputeJob(**cleaned)


class ComputeProviderDaemon:
    """Local provider daemon facade.

    The long-running service loop can be added around this class.  Keeping the
    core runtime synchronous and file-backed makes it easy to test and safe to
    run from CLI/systemd.
    """

    def __init__(
        self,
        config_path: Path | None = None,
        signer: Optional[Callable[[bytes], tuple[str, str]]] = None,
    ):
        self.config_path = config_path or provider_config_path()
        self.signer = signer
        self.config = self.load_config()

    # -- configuration -------------------------------------------------

    def load_config(self) -> ProviderConfig:
        if self.config_path.exists():
            try:
                return ProviderConfig.from_dict(json.loads(self.config_path.read_text()))
            except Exception:
                pass
        wallet = get_local_node_address(create=True) or ""
        provider_id = hashlib.sha256(wallet.encode("utf-8")).hexdigest()[:20] if wallet else ""
        return ProviderConfig(provider_id=provider_id, wallet_address=wallet)

    def save_config(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(self.config.to_dict(), indent=2, sort_keys=True))

    def set_enabled(self, enabled: bool) -> None:
        self.config.enabled = bool(enabled)
        if not self.config.wallet_address:
            self.config.wallet_address = get_local_node_address(create=True) or ""
        if not self.config.provider_id and self.config.wallet_address:
            self.config.provider_id = hashlib.sha256(
                self.config.wallet_address.encode("utf-8")
            ).hexdigest()[:20]
        self.save_config()

    # -- announcements -------------------------------------------------

    def build_announcement(self) -> dict:
        runtime = local_compute_runtime()
        profile = runtime.get("profile")
        share = normalize_compute_share(runtime.get("compute_share", 1.0))
        measured = float(runtime.get("tflops_measured", 0.0))
        effective = measured * share
        now = _now()
        active_jobs = len([j for j in self.list_jobs() if j.state in ("claimed", "running")])
        announcement = {
            "version": PROVIDER_VERSION,
            "provider_id": self.config.provider_id,
            "wallet_address": self.config.wallet_address,
            "endpoint": self.config.endpoint,
            "enabled": self.config.enabled,
            "chain_enabled": self.config.chain_enabled,
            "created_at": now,
            "expires_at": now + 90,
            "measured_tflops": round(measured, 4),
            "compute_share": round(share, 4),
            "effective_tflops": round(effective, 4),
            "active_jobs": active_jobs,
            "max_concurrent_jobs": self.config.max_concurrent_jobs,
            "settlement": "fiat_marketplace",
            "fiat_currency": self.config.fiat_currency,
            "price_per_inference_cents": self.config.price_per_inference_cents,
            "price_per_second_cents": self.config.price_per_second_cents,
            "connected_account_configured": bool(self.config.connected_account_id),
            "supported_task_types": list(self.config.supported_task_types),
            "execution_mode": self.config.execution_mode,
            "container_runtime": self.config.container_runtime,
            "allow_job_network": self.config.allow_job_network,
            "hardware": {
                "name": getattr(profile, "gpu_name", "Unknown") if profile else "Unknown",
                "backend": getattr(profile, "gpu_backend", "cpu") if profile else "cpu",
                "gpu_vram_mb": getattr(profile, "gpu_vram_mb", 0) if profile else 0,
                "ram_mb": getattr(profile, "ram_mb", 0) if profile else 0,
                "platform": getattr(profile, "platform", "") if profile else "",
                "arch": getattr(profile, "arch", "") if profile else "",
            },
        }
        announcement["announcement_hash"] = _sha256_json(announcement)
        signature, public_key = self._sign(_canonical_json(announcement))
        announcement["signature"] = signature
        announcement["public_key"] = public_key
        announcement["signature_scheme"] = "ed25519"
        return announcement

    def _sign(self, payload: bytes) -> tuple[str, str]:
        if self.signer:
            return self.signer(payload)
        try:
            from repryntt.economy.node_wallet import get_node_wallet

            wallet = get_node_wallet()
            if wallet and wallet.can_sign():
                return wallet.sign(payload).hex(), wallet.public_key.hex()
        except Exception:
            pass
        return "", ""

    @staticmethod
    def verify_announcement(announcement: dict) -> bool:
        signature = str(announcement.get("signature", ""))
        public_key = str(announcement.get("public_key", ""))
        if not signature or not public_key:
            return False
        unsigned = dict(announcement)
        unsigned.pop("signature", None)
        unsigned.pop("public_key", None)
        unsigned.pop("signature_scheme", None)
        expected_hash = unsigned.pop("announcement_hash", "")
        if expected_hash != _sha256_json(unsigned):
            return False
        unsigned["announcement_hash"] = expected_hash
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key))
            key.verify(bytes.fromhex(signature), _canonical_json(unsigned))
            return True
        except Exception:
            return False

    # -- jobs ----------------------------------------------------------

    def submit_local_job(
        self,
        *,
        buyer_address: str,
        task_type: str,
        payload: dict,
        max_price_cents: int = 0,
        escrow_id: str = "",
        model: str = "",
    ) -> ComputeJob:
        if task_type not in self.config.supported_task_types:
            raise ValueError(f"unsupported task_type: {task_type}")
        seed = {
            "buyer_address": buyer_address,
            "task_type": task_type,
            "payload": payload,
            "escrow_id": escrow_id,
            "created_at": _now(),
        }
        job = ComputeJob(
            job_id=_sha256_json(seed)[:24],
            buyer_address=buyer_address,
            task_type=task_type,
            payload=dict(payload or {}),
            max_price_cents=max(0, int(max_price_cents)),
            escrow_id=escrow_id,
            model=model,
            provider_id=self.config.provider_id,
        )
        self._write_job(job)
        return job

    def list_jobs(self) -> list[ComputeJob]:
        jobs = []
        for path in sorted(provider_jobs_dir().glob("*.json")):
            try:
                jobs.append(ComputeJob.from_dict(json.loads(path.read_text())))
            except Exception:
                continue
        return jobs

    def run_once(self) -> Optional[ComputeJob]:
        if not self.config.enabled:
            return None
        running = [j for j in self.list_jobs() if j.state in ("claimed", "running")]
        if len(running) >= self.config.max_concurrent_jobs:
            return None
        queued = [j for j in self.list_jobs() if j.state == "queued"]
        if not queued:
            return None
        job = sorted(queued, key=lambda j: j.created_at)[0]
        job.state = "running"
        job.claimed_at = _now()
        self._write_job(job)
        try:
            result = self._execute_job(job)
            job.result_hash = _sha256_json(result)
            receipt = self._build_receipt(job, result)
            job.receipt_hash = receipt["receipt_hash"]
            job.state = "completed"
        except Exception as exc:
            job.error = str(exc)
            job.state = "failed"
        job.completed_at = _now()
        self._write_job(job)
        return job

    def _execute_job(self, job: ComputeJob) -> dict:
        if job.task_type == "health_check":
            return {
                "ok": True,
                "provider_id": self.config.provider_id,
                "timestamp": _now(),
            }
        if job.task_type == "text_generation":
            return self._execute_text_generation(job)
        if job.task_type == "embedding":
            text = str(job.payload.get("text", ""))
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vector = [round((b / 255.0), 6) for b in digest[:16]]
            return {"embedding": vector, "dimensions": len(vector), "model": "sha256-local-v1"}
        raise ValueError(f"unsupported task_type: {job.task_type}")

    def _execute_text_generation(self, job: ComputeJob) -> dict:
        if self.config.execution_mode != "local_llm":
            raise RuntimeError("text_generation requires execution_mode=local_llm")
        endpoint = os.environ.get(
            "REPRYNTT_LLM_ENDPOINT",
            "http://127.0.0.1:8080/v1/chat/completions",
        )
        prompt = str(job.payload.get("prompt", ""))
        if not prompt:
            raise ValueError("text_generation payload requires prompt")
        body = {
            "model": job.model or os.environ.get("REPRYNTT_LLM_MODEL", "local"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": float(job.payload.get("temperature", 0.2)),
            "max_tokens": int(job.payload.get("max_tokens", 256)),
        }
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.config.job_timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = ""
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception:
            content = json.dumps(data, sort_keys=True)
        return {
            "content": content,
            "model": body["model"],
            "raw_provider": "local_llm",
        }

    def _build_receipt(self, job: ComputeJob, result: dict) -> dict:
        receipt = {
            "version": PROVIDER_VERSION,
            "job_id": job.job_id,
            "provider_id": self.config.provider_id,
            "wallet_address": self.config.wallet_address,
            "task_type": job.task_type,
            "escrow_id": job.escrow_id,
            "started_at": job.claimed_at,
            "completed_at": _now(),
            "result_hash": _sha256_json(result),
        }
        receipt["receipt_hash"] = _sha256_json(receipt)
        signature, public_key = self._sign(_canonical_json(receipt))
        receipt["signature"] = signature
        receipt["public_key"] = public_key
        receipt["signature_scheme"] = "ed25519"
        receipt_path = provider_jobs_dir() / f"{job.job_id}.receipt.json"
        receipt_path.write_text(json.dumps({"receipt": receipt, "result": result}, indent=2, sort_keys=True))
        return receipt

    def _write_job(self, job: ComputeJob) -> None:
        path = provider_jobs_dir() / f"{job.job_id}.json"
        path.write_text(json.dumps(job.to_dict(), indent=2, sort_keys=True))

    def status(self) -> dict:
        runtime = local_compute_runtime()
        jobs = self.list_jobs()
        counts: dict[str, int] = {}
        for job in jobs:
            counts[job.state] = counts.get(job.state, 0) + 1
        return {
            "provider": self.config.to_dict(),
            "runtime": {
                "measured_tflops": round(float(runtime["tflops_measured"]), 4),
                "compute_share": round(float(runtime["compute_share"]), 4),
                "effective_tflops": round(float(runtime["tflops_effective"]), 4),
            },
            "job_counts": counts,
            "announcement": self.build_announcement(),
        }
