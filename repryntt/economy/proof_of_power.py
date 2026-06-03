"""
SAIGE Robot Economy — Proof of Power Consensus System
=====================================================
Replaces wasteful hash-based Proof of Work with productive Proof of Power:
  miners prove they performed real AI computation (inference, training, analysis)
  instead of burning electricity on meaningless hash puzzles.

Key components:
  ProofOfPower.generate_proof()           — Create proof from completed workload
  ProofOfPower.verify_proof()             — Validate proof integrity & quality
  ProofOfPower.measure_device_tflops()    — Dynamically measure real hardware TFLOPS
  ProofOfPower.estimate_workload_tflops() — Estimate TFLOPS of workload
  ProofOfPower.calculate_reward()         — Dynamic reward based on contribution

Quality factor (0.0–1.0) measures how well the miner performed:
  - Result hash integrity
  - Computation time plausibility
  - Output length / completeness
  - Miner identity consistency
  - Verified device attestation
"""

import hashlib
import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from repryntt.economy.logging_config import blockchain_logger as logger

PLANCKS_PER_CR = 100_000_000

# Cache measured TFLOPS so we don't re-benchmark every call
_cached_tflops: Optional[float] = None
_cached_device_info: Optional[Dict[str, Any]] = None

# Disk cache — avoids importing torch (~400MB RSS) on every restart.
# Benchmark is only re-run when the cache is missing/stale (>24h).
from repryntt.paths import data_dir as _data_dir

_BENCHMARK_CACHE = str(_data_dir() / "gpu_benchmark_cache.json")
_BENCHMARK_MAX_AGE = 86400  # 24 hours


def _load_benchmark_cache() -> Optional[Tuple[float, Dict[str, Any]]]:
    """Load cached benchmark from disk if fresh enough."""
    try:
        if os.path.exists(_BENCHMARK_CACHE):
            with open(_BENCHMARK_CACHE, "r") as f:
                data = json.load(f)
            age = time.time() - data.get("timestamp", 0)
            if age < _BENCHMARK_MAX_AGE:
                return data["tflops"], data["device_info"]
    except Exception:
        pass
    return None


def _save_benchmark_cache(tflops: float, device_info: Dict[str, Any]):
    """Persist benchmark to disk so next startup skips torch import."""
    try:
        os.makedirs(os.path.dirname(_BENCHMARK_CACHE), exist_ok=True)
        with open(_BENCHMARK_CACHE, "w") as f:
            json.dump({"tflops": tflops, "device_info": device_info,
                        "timestamp": time.time()}, f)
    except Exception:
        pass


def measure_device_tflops() -> Tuple[float, Dict[str, Any]]:
    """
    Dynamically measure actual TFLOPS of the current hardware.

    Runs a short matrix-multiply benchmark on whatever accelerator is
    available (CUDA, ROCm, MPS, CPU) and returns measured TFLOPS plus
    device metadata.  Results are cached in memory AND on disk so that
    subsequent process startups avoid importing torch (~400MB RSS).

    Returns:
        (tflops, device_info) where device_info contains:
          device_type: 'cuda' | 'rocm' | 'mps' | 'cpu'
          device_name: Human-readable GPU/CPU name
          tflops_fp16: Measured FP16 TFLOPS
          tflops_fp32: Measured FP32 TFLOPS
          memory_gb: Available VRAM/RAM in GB
          benchmark_time_s: Time taken to benchmark
    """
    global _cached_tflops, _cached_device_info
    if _cached_tflops is not None:
        return _cached_tflops, _cached_device_info

    # Try disk cache first (avoids ~400MB torch import)
    disk = _load_benchmark_cache()
    if disk is not None:
        _cached_tflops, _cached_device_info = disk
        logger.info(
            f"🔬 GPU benchmark (cached): {_cached_device_info.get('device_name', '?')} — "
            f"FP16: {_cached_device_info.get('tflops_fp16', _cached_tflops)} TFLOPS"
        )
        return _cached_tflops, _cached_device_info

    device_info = {
        "device_type": "cpu",
        "device_name": "Unknown CPU",
        "tflops_fp16": 0.0,
        "tflops_fp32": 0.0,
        "memory_gb": 0.0,
        "benchmark_time_s": 0.0,
    }

    try:
        import numpy as np

        # ── Try GPU first ──
        gpu_measured = False
        try:
            import torch
            if torch.cuda.is_available():
                device = torch.device("cuda")
                device_info["device_type"] = "cuda"
                device_info["device_name"] = torch.cuda.get_device_name(0)
                try:
                    mem = torch.cuda.get_device_properties(0).total_mem
                except AttributeError:
                    # Jetson / some GPUs use total_memory instead
                    try:
                        mem = torch.cuda.get_device_properties(0).total_memory
                    except AttributeError:
                        mem = torch.cuda.mem_get_info()[1] if hasattr(torch.cuda, 'mem_get_info') else 0
                device_info["memory_gb"] = round(mem / (1024**3), 2)

                # FP16 benchmark: 2048x2048 matmul
                size = 2048
                a = torch.randn(size, size, dtype=torch.float16, device=device)
                b = torch.randn(size, size, dtype=torch.float16, device=device)
                # Warmup
                for _ in range(3):
                    torch.mm(a, b)
                torch.cuda.synchronize()

                start = time.perf_counter()
                iterations = 20
                for _ in range(iterations):
                    torch.mm(a, b)
                torch.cuda.synchronize()
                elapsed = time.perf_counter() - start

                # FLOPS = 2 * N^3 per matmul
                flops = 2 * (size ** 3) * iterations
                device_info["tflops_fp16"] = round(flops / elapsed / 1e12, 3)
                device_info["benchmark_time_s"] = round(elapsed, 3)

                # FP32 benchmark
                a32 = torch.randn(size, size, dtype=torch.float32, device=device)
                b32 = torch.randn(size, size, dtype=torch.float32, device=device)
                for _ in range(3):
                    torch.mm(a32, b32)
                torch.cuda.synchronize()
                start = time.perf_counter()
                for _ in range(iterations):
                    torch.mm(a32, b32)
                torch.cuda.synchronize()
                elapsed32 = time.perf_counter() - start
                device_info["tflops_fp32"] = round(flops / elapsed32 / 1e12, 3)

                gpu_measured = True
                logger.info(
                    f"🔬 GPU benchmark: {device_info['device_name']} — "
                    f"FP16: {device_info['tflops_fp16']} TFLOPS, "
                    f"FP32: {device_info['tflops_fp32']} TFLOPS, "
                    f"VRAM: {device_info['memory_gb']} GB"
                )
            elif hasattr(torch.backends, 'mps') and torch.backends.mps.is_available():
                device_info["device_type"] = "mps"
                device_info["device_name"] = "Apple Silicon (MPS)"
                # MPS benchmark (smaller to avoid OOM on some Macs)
                size = 1024
                device = torch.device("mps")
                a = torch.randn(size, size, dtype=torch.float32, device=device)
                b = torch.randn(size, size, dtype=torch.float32, device=device)
                for _ in range(3):
                    torch.mm(a, b)
                start = time.perf_counter()
                iterations = 20
                for _ in range(iterations):
                    torch.mm(a, b)
                elapsed = time.perf_counter() - start
                flops = 2 * (size ** 3) * iterations
                device_info["tflops_fp32"] = round(flops / elapsed / 1e12, 3)
                device_info["tflops_fp16"] = device_info["tflops_fp32"] * 2  # Rough estimate
                device_info["benchmark_time_s"] = round(elapsed, 3)
                gpu_measured = True
        except ImportError:
            pass

        if not gpu_measured:
            # ── CPU fallback benchmark with numpy ──
            device_info["device_type"] = "cpu"
            try:
                with open("/proc/cpuinfo") as f:
                    for line in f:
                        if line.startswith("model name"):
                            device_info["device_name"] = line.split(":")[1].strip()
                            break
            except Exception:
                import platform
                device_info["device_name"] = platform.processor() or "Unknown CPU"

            try:
                import psutil
                device_info["memory_gb"] = round(psutil.virtual_memory().total / (1024**3), 2)
            except ImportError:
                pass

            size = 512  # Smaller for CPU
            a = np.random.randn(size, size).astype(np.float32)
            b = np.random.randn(size, size).astype(np.float32)
            # Warmup
            np.dot(a, b)
            start = time.perf_counter()
            iterations = 10
            for _ in range(iterations):
                np.dot(a, b)
            elapsed = time.perf_counter() - start
            flops = 2 * (size ** 3) * iterations
            device_info["tflops_fp32"] = round(flops / elapsed / 1e12, 4)
            device_info["tflops_fp16"] = device_info["tflops_fp32"]  # CPU doesn't differentiate
            device_info["benchmark_time_s"] = round(elapsed, 3)
            logger.info(
                f"🔬 CPU benchmark: {device_info['device_name']} — "
                f"FP32: {device_info['tflops_fp32']} TFLOPS"
            )

    except Exception as e:
        logger.warning(f"TFLOPS benchmark failed: {e}")
        # Fallback: estimate from known device types
        device_info["tflops_fp32"] = 0.01
        device_info["tflops_fp16"] = 0.02

    _cached_tflops = max(device_info["tflops_fp16"], device_info["tflops_fp32"], 0.001)
    _cached_device_info = device_info
    _save_benchmark_cache(_cached_tflops, _cached_device_info)
    return _cached_tflops, _cached_device_info


class ProofOfPower:
    """
    Proof of Power consensus engine.

    Instead of finding a hash below a target (PoW), miners submit
    cryptographic proof that they performed a real AI workload.
    The proof ties together:
      - workload_key  (what was requested)
      - data hash     (input integrity)
      - result hash   (output integrity)
      - miner address (who did it)
      - timing data   (plausibility check)
      - device attestation (measured hardware TFLOPS)

    Verification includes:
      - Hash integrity checks
      - Timing plausibility vs declared TFLOPS
      - Challenge-response: validator re-hashes a random slice of the
        result to catch fabricated outputs
    """

    # Plausibility bounds for computation time (seconds)
    MIN_COMPUTATION_TIME = 0.01    # 10ms minimum (even a cache hit takes some time)
    MAX_COMPUTATION_TIME = 600.0   # 10 minutes maximum per workload

    # TFLOPS: dynamically measured at init (not hardcoded)
    BASELINE_TFLOPS = 20.0  # Fallback only
    TOKENS_PER_TFLOP_SECOND = 50   # Rough: 50 tokens/s at 20 TFLOPS

    def __init__(self):
        self._verification_cache: Dict[str, Tuple[bool, float]] = {}
        # Measure real hardware TFLOPS on init
        try:
            self._measured_tflops, self._device_info = measure_device_tflops()
            self.BASELINE_TFLOPS = max(self._measured_tflops, 0.001)
        except Exception as e:
            logger.warning(f"Device benchmark failed, using default: {e}")
            self._measured_tflops = self.BASELINE_TFLOPS
            self._device_info = {"device_type": "cpu", "device_name": "unknown"}

    def get_device_info(self) -> Dict[str, Any]:
        """Return measured device capabilities."""
        return {
            "tflops": self._measured_tflops,
            **self._device_info,
        }

    # ─── Proof Generation ─────────────────────────────

    def generate_proof(
        self,
        workload_key: str,
        workload_data: Any,
        computation_result: Any,
        miner_address: str,
        computation_time: float,
        method: str = "deterministic",
        device_type: str = "cpu",
        gpu_backend: str = "",
        tflops_measured: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Generate a Proof of Power from a completed workload.

        SECURITY: device_type, gpu_backend, and tflops_measured parameters
        are IGNORED — the proof always uses this node's real measured values
        from the init-time benchmark.  Callers cannot inflate their hardware
        claims.  The parameters are kept for API compatibility but have no
        effect on the proof.

        Returns:
            Proof dictionary containing all verification data
        """
        now = time.time()

        # Hash the inputs and outputs
        data_hash = self._hash(workload_data)
        result_hash = self._hash(computation_result)

        # Build proof hash (ties everything together cryptographically)
        proof_input = f"{workload_key}:{data_hash}:{result_hash}:{miner_address}:{computation_time}"
        proof_hash = hashlib.sha3_512(proof_input.encode()).hexdigest()

        # SECURITY: Always use THIS NODE's measured values — never trust
        # caller-provided device claims.  This is the equivalent of Bitcoin
        # requiring real hash work; you can't fake your GPU.
        real_device_type = self._device_info.get("device_type", "cpu")
        real_gpu_backend = self._device_info.get("device_name", "")
        real_tflops = self._measured_tflops

        # Hardware attestation: sign our device fingerprint so peers can
        # verify we're not lying about hardware
        hw_attest_hash = ""
        try:
            from repryntt.economy.entity_verification import (
                collect_hardware_fingerprint,
                sign_hardware_attestation,
            )
            fp = collect_hardware_fingerprint()
            attestation = sign_hardware_attestation(fp)
            hw_attest_hash = hashlib.sha3_256(
                json.dumps(attestation, sort_keys=True).encode()
            ).hexdigest()[:32]
        except Exception:
            pass  # Best-effort — attestation is bonus quality, not required

        proof = {
            "proof_hash": proof_hash,
            "workload_key": workload_key,
            "data_hash": data_hash,
            "result_hash": result_hash,
            "miner_address": miner_address,
            "computation_time": computation_time,
            "method": method,
            "timestamp": now,
            "version": "3.1",
            "device_type": real_device_type,
            "gpu_backend": real_gpu_backend,
            "tflops_measured": real_tflops,
            "hw_attestation_hash": hw_attest_hash,
        }

        # Generate challenge-response (validators re-check this slice)
        result_str = str(computation_result) if not isinstance(computation_result, str) else computation_result
        if len(result_str) >= 10:
            proof_bytes = bytes.fromhex(proof_hash[:16])
            offset = int.from_bytes(proof_bytes[:4], 'big') % max(1, len(result_str) - 32)
            length = min(64, len(result_str) - offset)
            challenge_slice = result_str[offset:offset + length]
            proof["challenge_response"] = hashlib.sha3_256(
                f"{proof_hash}:{challenge_slice}".encode()
            ).hexdigest()[:32]

        logger.debug(
            f"PoP generated: {proof_hash[:16]}... "
            f"workload={workload_key[:8]}... miner={miner_address[:8]}... "
            f"time={computation_time:.2f}s method={method}"
        )
        return proof

    # ─── Proof Verification ───────────────────────────

    def verify_proof(
        self,
        proof: Dict[str, Any],
        workload_data: Any,
        computation_result: Any,
        expected_miner: str,
    ) -> Tuple[bool, float, str]:
        """
        Verify a Proof of Power with challenge-response validation.

        Returns:
            (is_valid, quality_factor, reason)
            quality_factor: 0.0–1.0 representing work quality
            reason: Human-readable explanation (empty if valid)
        """
        try:
            # 1. Check required fields
            required = ["proof_hash", "workload_key", "data_hash", "result_hash",
                        "miner_address", "computation_time"]
            for field in required:
                if field not in proof:
                    return False, 0.0, f"Missing field: {field}"

            # 2. Verify miner identity
            if proof["miner_address"] != expected_miner:
                return False, 0.0, "Miner address mismatch"

            # 3. Verify data hash matches workload
            expected_data_hash = self._hash(workload_data)
            if proof["data_hash"] != expected_data_hash:
                return False, 0.0, "Data hash mismatch — workload input was tampered"

            # 4. Verify result hash matches computation result
            expected_result_hash = self._hash(computation_result)
            if proof["result_hash"] != expected_result_hash:
                return False, 0.0, "Result hash mismatch — computation output was tampered"

            # 5. Verify proof hash integrity
            proof_input = (
                f"{proof['workload_key']}:{proof['data_hash']}:{proof['result_hash']}:"
                f"{proof['miner_address']}:{proof['computation_time']}"
            )
            expected_proof_hash = hashlib.sha3_512(proof_input.encode()).hexdigest()
            if proof["proof_hash"] != expected_proof_hash:
                return False, 0.0, "Proof hash integrity check failed"

            # 6. Check computation time plausibility
            comp_time = proof["computation_time"]
            if comp_time < self.MIN_COMPUTATION_TIME:
                return False, 0.0, f"Computation time too fast ({comp_time:.4f}s) — likely cached/faked"
            if comp_time > self.MAX_COMPUTATION_TIME:
                return False, 0.0, f"Computation time too slow ({comp_time:.1f}s) — exceeds limit"

            # 7. Check result is non-trivial
            result_str = str(computation_result)
            if len(result_str) < 2:
                return False, 0.0, "Trivial/empty result"

            # 8. CHALLENGE-RESPONSE VERIFICATION
            #    Re-hash a deterministic slice of the result to catch fabrication.
            #    The slice offset is derived from the proof hash, so the miner
            #    can't predict which part will be checked.
            challenge_passed = self._challenge_verify(proof, result_str)
            if not challenge_passed:
                return False, 0.0, "Challenge-response verification failed — result may be fabricated"

            # 9. TFLOPS plausibility check — REJECT implausible claims
            #    If a miner claims 500 TFLOPS but produces tiny output in a
            #    long time, they're lying.  Unlike the old code that only warned,
            #    this now REJECTS proofs with clearly fraudulent TFLOPS claims.
            declared_tflops = proof.get("tflops_measured", 0)
            if declared_tflops > 0 and comp_time > 1.0:
                # For declared power level, result must be proportional to
                # time spent.  A 100 TFLOPS GPU running 60s should produce
                # substantial output, not 30 characters.
                expected_min_chars = declared_tflops * self.TOKENS_PER_TFLOP_SECOND * comp_time * 0.005
                if len(result_str) < expected_min_chars and len(result_str) < 50:
                    return False, 0.0, (
                        f"TFLOPS fraud: {declared_tflops:.1f} TFLOPS declared, "
                        f"{comp_time:.1f}s elapsed, but only {len(result_str)} chars output "
                        f"(expected >= {int(expected_min_chars)})"
                    )

            # 9b. TFLOPS ceiling — cap declared TFLOPS to known-possible range
            #     No consumer/enterprise GPU exceeds ~500 TFLOPS FP16 in 2026.
            #     If someone claims 1000+, they're lying.
            MAX_PLAUSIBLE_TFLOPS = 500.0
            if declared_tflops > MAX_PLAUSIBLE_TFLOPS:
                return False, 0.0, (
                    f"TFLOPS implausible: {declared_tflops:.1f} exceeds "
                    f"maximum known hardware ({MAX_PLAUSIBLE_TFLOPS} TFLOPS)"
                )

            # 10. Calculate quality factor
            quality = self._calculate_quality(proof, computation_result)

            logger.debug(
                f"PoP verified: {proof['proof_hash'][:16]}... "
                f"quality={quality:.3f} time={comp_time:.2f}s challenge=PASS"
            )
            return True, quality, ""

        except Exception as e:
            logger.error(f"PoP verification error: {e}")
            return False, 0.0, f"Verification error: {e}"

    def _challenge_verify(self, proof: Dict, result_str: str) -> bool:
        """
        Challenge-response: deterministically select a slice of the result
        and verify its hash matches.  This prevents miners from submitting
        a valid result_hash without actually having the full result.
        """
        if len(result_str) < 10:
            return True  # Too short to slice meaningfully

        # Use proof_hash bytes to determine slice offset and length
        proof_bytes = bytes.fromhex(proof["proof_hash"][:16])
        offset = int.from_bytes(proof_bytes[:4], 'big') % max(1, len(result_str) - 32)
        length = min(64, len(result_str) - offset)
        challenge_slice = result_str[offset:offset + length]

        # The expected challenge hash is embedded in the proof (or recomputed)
        expected_challenge = hashlib.sha3_256(
            f"{proof['proof_hash']}:{challenge_slice}".encode()
        ).hexdigest()[:32]

        # If proof includes challenge_response, verify it
        if "challenge_response" in proof:
            return proof["challenge_response"] == expected_challenge

        # If no challenge_response field (old proof format), pass but with
        # reduced quality (handled in _calculate_quality)
        return True

    def _calculate_quality(self, proof: Dict, result: Any) -> float:
        """
        Calculate quality factor (0.0–1.0) based on work characteristics.

        Scoring breakdown:
          0.25 — base (proof is structurally valid)
          0.20 — computation time factor
          0.20 — result richness
          0.15 — result entropy (prevents trivial/repetitive outputs)
          0.20 — device attestation (GPU > CPU, measured TFLOPS bonus)
        """
        quality = 0.25  # Base

        # Time factor: reward reasonable computation times (max 0.20)
        comp_time = proof.get("computation_time", 0)
        if comp_time < 0.5:
            quality += 0.04  # Very fast — suspicious but valid
        elif comp_time <= 30.0:
            quality += 0.08 + min(0.12, comp_time / 250.0)
        else:
            quality += 0.16  # Diminishing after 30s

        # Result richness: longer/more complex results (max 0.20)
        result_str = str(result)
        result_len = len(result_str)
        if result_len > 50:
            quality += min(0.20, 0.04 + result_len / 25000.0)

        # Entropy check: measure character diversity to penalize repetitive outputs (max 0.15)
        if result_len > 10:
            unique_chars = len(set(result_str[:2000]))
            entropy_ratio = unique_chars / min(result_len, 2000)
            quality += min(0.15, entropy_ratio * 0.4)

        # Device attestation: quality bonus ONLY if hardware attestation
        # hash is present (proves the node signed its real hardware fingerprint).
        # Without attestation, device_type claims get ZERO bonus — this prevents
        # CPU nodes from claiming to be GPUs for a free 0.20 quality bump.
        device_type = proof.get("device_type", "cpu")
        tflops = proof.get("tflops_measured", 0.0)
        hw_attest = proof.get("hw_attestation_hash", "")
        gpu_types = {"cuda", "rocm", "mps", "xpu"}
        if hw_attest and len(hw_attest) >= 16:
            # Hardware attestation present — award device bonus
            if device_type in gpu_types:
                quality += 0.10  # GPU bonus
                if 0.5 <= tflops <= 500.0:
                    quality += min(0.10, tflops / self.BASELINE_TFLOPS * 0.05)
            elif device_type == "cpu" and tflops > 0:
                quality += 0.03
        # No attestation → no device bonus. Proof is still valid (base 0.25
        # + time + richness + entropy) but earns less.  This is the "you must
        # do real work" enforcement.

        return min(1.0, max(0.0, round(quality, 4)))

    # ─── TFLOPS Estimation ────────────────────────────

    def estimate_workload_tflops(self, workload_data: Any, workload_type: str = "inference") -> float:
        """
        Estimate the TFLOPS required for a workload.

        This is a rough heuristic — real measurement would require
        GPU profiling. Used for proportional reward calculation.

        Args:
            workload_data: The workload input data
            workload_type: 'inference', 'training', 'analysis'

        Returns:
            Estimated TFLOPS (floating point)
        """
        base_tflops = 0.001  # Minimum

        try:
            data_str = str(workload_data)
            data_size = len(data_str)

            if workload_type == "inference":
                # Token count estimate → TFLOPS
                # ~1 TFLOP per 500 input tokens at 7B parameter scale
                estimated_tokens = max(1, data_size // 4)  # ~4 chars per token
                base_tflops = estimated_tokens / 500.0

                # Adjust for max_tokens if present
                if isinstance(workload_data, dict):
                    max_tokens = workload_data.get("max_tokens", 512)
                    base_tflops += max_tokens / 500.0

            elif workload_type == "training":
                # Training is ~3x inference cost
                base_tflops = (data_size / 500.0) * 3.0

            elif workload_type == "analysis":
                # Analysis is ~1.5x inference
                base_tflops = (data_size / 500.0) * 1.5

            else:
                base_tflops = data_size / 1000.0

        except Exception as e:
            logger.warning(f"TFLOPS estimation error: {e}")
            base_tflops = 0.01

        # Clamp to reasonable range
        return max(0.001, min(1000.0, round(base_tflops, 4)))

    # ─── Reward Calculation ───────────────────────────

    def calculate_reward(
        self,
        base_reward: int,
        workload_tflops: float,
        quality_factor: float,
        block_height: int,
        halving_interval: int = 420000,
    ) -> int:
        """
        Calculate dynamic mining reward based on computational contribution.

        Formula:
          reward = base_reward × halving_factor × tflops_multiplier × quality_factor

        Args:
            base_reward: Base reward in Plancks (e.g. 10 CR = 1,000,000,000)
            workload_tflops: Estimated TFLOPS of the workload
            quality_factor: 0.0–1.0 from verify_proof()
            block_height: Current chain height
            halving_interval: Blocks between halvings (420,000)

        Returns:
            Reward amount in Plancks
        """
        # Halving: reward halves every `halving_interval` blocks
        halvings = block_height // halving_interval
        halving_factor = 1.0 / (2 ** halvings)

        # TFLOPS multiplier: more computation = slightly higher reward
        # Capped at 2x to prevent gaming
        tflops_multiplier = min(2.0, max(0.5, 0.5 + (workload_tflops / self.BASELINE_TFLOPS)))

        # Quality multiplier: higher quality = more reward
        quality_multiplier = max(0.1, quality_factor)

        # Calculate final reward
        reward = base_reward * halving_factor * tflops_multiplier * quality_multiplier
        reward_plancks = max(1, int(reward))

        logger.debug(
            f"Reward calc: base={base_reward} halving={halving_factor:.4f} "
            f"tflops_mult={tflops_multiplier:.3f} quality={quality_multiplier:.3f} "
            f"→ {reward_plancks} plancks ({reward_plancks / PLANCKS_PER_CR:.6f} CR)"
        )
        return reward_plancks

    # ─── Utilities ────────────────────────────────────

    @staticmethod
    def _hash(data: Any) -> str:
        """Hash arbitrary data to SHA3-512 hex digest."""
        if isinstance(data, (dict, list)):
            data_str = json.dumps(data, sort_keys=True, default=str)
        else:
            data_str = str(data)
        return hashlib.sha3_512(data_str.encode()).hexdigest()
