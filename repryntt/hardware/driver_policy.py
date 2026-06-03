"""
repryntt.hardware.driver_policy — Learnable MLP Navigation Policy.

Replaces the Q-table and hand-coded reactive rules with a small neural
network that learns to drive from experience.

Architecture:
    Input: 50-dim feature vector (from YOLO + stereo + temporal + goal)
    Hidden: [128, 64, 32] with ReLU + dropout
    Output: 5 action logits + 1 uncertainty scalar

    Total: ~14K parameters — runs in <1ms on Jetson.

Training:
    Phase 1 (behavior cloning): Learn from Gemini's decisions in JSONL logs
    Phase 2 (offline RL): Learn from (state, action, reward) tuples
    Phase 3 (on-policy): Drive, Gemini intervenes only on high uncertainty

The uncertainty output is key: when the policy is unsure, we fall back
to Gemini. Every Gemini intervention becomes high-value training data.
Over time, uncertainty decreases → fewer Gemini calls → self-evolving driver.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Action space (matches nav_cortex.py)
ACTION_NAMES = ["forward", "backward", "turn_left", "turn_right", "stop"]
NUM_ACTIONS = len(ACTION_NAMES)
FEATURE_DIM = 50

# Uncertainty threshold: above this, fall back to Gemini/reactive
DEFAULT_UNCERTAINTY_THRESHOLD = 0.4

# ── Intervention tracker ─────────────────────────────────────────────

@dataclass
class InterventionStats:
    """Tracks how often Gemini has to intervene vs. the policy deciding alone."""
    policy_decisions: int = 0
    gemini_interventions: int = 0
    total_steps: int = 0
    session_start: float = 0.0

    @property
    def intervention_rate(self) -> float:
        """Fraction of steps requiring Gemini. Lower = better policy."""
        if self.total_steps == 0:
            return 1.0
        return self.gemini_interventions / self.total_steps

    @property
    def autonomy_rate(self) -> float:
        """Fraction of steps the policy handles alone. Higher = better."""
        return 1.0 - self.intervention_rate

    def record_policy_decision(self):
        self.policy_decisions += 1
        self.total_steps += 1

    def record_gemini_intervention(self):
        self.gemini_interventions += 1
        self.total_steps += 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_decisions": self.policy_decisions,
            "gemini_interventions": self.gemini_interventions,
            "total_steps": self.total_steps,
            "intervention_rate": round(self.intervention_rate, 4),
            "autonomy_rate": round(self.autonomy_rate, 4),
            "session_start": self.session_start,
        }


# ── PyTorch MLP ──────────────────────────────────────────────────────

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False


if TORCH_AVAILABLE:
    class DriverMLP(nn.Module):
        """Small MLP policy: features → action logits + uncertainty.
        
        ~14K params. Runs in <1ms on Jetson CPU, <0.1ms on GPU.
        
        The uncertainty head is trained with a simple auxiliary loss:
        high uncertainty when the policy's chosen action disagrees with
        the teacher (Gemini), low uncertainty when they agree.
        """

        def __init__(self, input_dim: int = FEATURE_DIM,
                     num_actions: int = NUM_ACTIONS,
                     hidden_dims: Tuple[int, ...] = (128, 64, 32)):
            super().__init__()

            layers = []
            prev_dim = input_dim
            for h in hidden_dims:
                layers.append(nn.Linear(prev_dim, h))
                layers.append(nn.ReLU())
                layers.append(nn.Dropout(0.1))
                prev_dim = h
            self.backbone = nn.Sequential(*layers)

            # Action head: logits over 5 actions
            self.action_head = nn.Linear(prev_dim, num_actions)

            # Uncertainty head: single scalar (sigmoid → 0-1)
            self.uncertainty_head = nn.Linear(prev_dim, 1)

        def forward(self, x: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
            """Returns (action_logits, uncertainty)."""
            features = self.backbone(x)
            logits = self.action_head(features)
            uncertainty = torch.sigmoid(self.uncertainty_head(features))
            return logits, uncertainty

        def predict(self, x: torch.Tensor) -> Tuple[int, float, float]:
            """Single inference: returns (action_id, confidence, uncertainty)."""
            self.eval()
            with torch.no_grad():
                logits, unc = self.forward(x.unsqueeze(0) if x.dim() == 1 else x)
                probs = F.softmax(logits, dim=-1)
                action = int(torch.argmax(probs, dim=-1).item())
                confidence = float(probs[0, action].item())
                uncertainty = float(unc[0, 0].item())
                return action, confidence, uncertainty


# ── Numpy-only fallback MLP (no PyTorch needed at inference) ────────

class NumpyDriverMLP:
    """Pure-numpy inference for the driver MLP.
    
    Loads weights exported from PyTorch. Zero dependency at inference time.
    Useful if we want to avoid loading PyTorch for every explorer step.
    """

    def __init__(self):
        self.weights: List[np.ndarray] = []
        self.biases: List[np.ndarray] = []
        self.action_w: Optional[np.ndarray] = None
        self.action_b: Optional[np.ndarray] = None
        self.unc_w: Optional[np.ndarray] = None
        self.unc_b: Optional[np.ndarray] = None
        self._loaded = False

    def load(self, path: str) -> bool:
        """Load numpy weights exported from PyTorch model."""
        try:
            data = np.load(path, allow_pickle=True)
            self.weights = [data[f"w{i}"] for i in range(data["num_layers"])]
            self.biases = [data[f"b{i}"] for i in range(data["num_layers"])]
            self.action_w = data["action_w"]
            self.action_b = data["action_b"]
            self.unc_w = data["unc_w"]
            self.unc_b = data["unc_b"]
            self._loaded = True
            total_params = sum(w.size + b.size for w, b in zip(self.weights, self.biases))
            total_params += self.action_w.size + self.action_b.size
            total_params += self.unc_w.size + self.unc_b.size
            logger.info(f"🧠 Driver MLP loaded: {total_params} params from {path}")
            return True
        except Exception as e:
            logger.debug(f"Failed to load numpy MLP: {e}")
            return False

    def predict(self, x: np.ndarray) -> Tuple[int, float, float]:
        """Inference: feature vector → (action, confidence, uncertainty)."""
        if not self._loaded:
            return 4, 0.0, 1.0  # stop with max uncertainty

        # Forward pass through backbone
        h = x.astype(np.float32)
        for w, b in zip(self.weights, self.biases):
            h = h @ w.T + b
            h = np.maximum(h, 0)  # ReLU

        # Action head
        logits = h @ self.action_w.T + self.action_b
        # Softmax
        exp_logits = np.exp(logits - np.max(logits))
        probs = exp_logits / exp_logits.sum()
        action = int(np.argmax(probs))
        confidence = float(probs[action])

        # Uncertainty head
        unc_raw = h @ self.unc_w.T + self.unc_b
        uncertainty = float(1.0 / (1.0 + np.exp(-unc_raw[0])))  # sigmoid

        return action, confidence, uncertainty


# ── Driver Policy (combines model + intervention tracking) ───────────

class DriverPolicy:
    """The learnable navigation policy.
    
    Wraps the MLP model, handles loading/saving, tracks interventions,
    and provides the decide() interface that Explorer calls.
    """

    def __init__(self, model_dir: Optional[str] = None,
                 uncertainty_threshold: float = DEFAULT_UNCERTAINTY_THRESHOLD):
        if model_dir is None:
            model_dir = str(Path.home() / ".repryntt" / "models")
        self.model_dir = Path(model_dir)
        self.model_dir.mkdir(parents=True, exist_ok=True)

        self.uncertainty_threshold = uncertainty_threshold
        self.stats = InterventionStats(session_start=time.time())

        # Try to load existing model
        self._torch_model: Optional[Any] = None  # DriverMLP if available
        self._numpy_model = NumpyDriverMLP()
        self._available = False
        self._load()

    def _load(self) -> bool:
        """Load the best available model."""
        numpy_path = self.model_dir / "driver_policy.npz"
        torch_path = self.model_dir / "driver_policy.pt"

        # Try numpy first (faster loading, no PyTorch import)
        if numpy_path.exists():
            if self._numpy_model.load(str(numpy_path)):
                self._available = True
                return True

        # Try PyTorch
        if TORCH_AVAILABLE and torch_path.exists():
            try:
                model = DriverMLP()
                model.load_state_dict(torch.load(str(torch_path), map_location="cpu",
                                                 weights_only=True))
                model.eval()
                self._torch_model = model
                self._available = True
                logger.info(f"🧠 Driver policy loaded from {torch_path}")
                return True
            except Exception as e:
                logger.debug(f"Failed to load torch model: {e}")

        logger.info("🧠 No trained driver policy found — will use reactive fallback + collect training data")
        return False

    @property
    def available(self) -> bool:
        return self._available

    def decide(self, feature_vector: np.ndarray) -> Dict[str, Any]:
        """Main decision function.
        
        Returns:
            {
                "action": str,          # "forward", "turn_left", etc.
                "action_id": int,
                "confidence": float,
                "uncertainty": float,   # 0=certain, 1=lost
                "method": "policy",
                "needs_gemini": bool,   # True if uncertainty > threshold
            }
        """
        if not self._available:
            return {
                "action": "stop",
                "action_id": 4,
                "confidence": 0.0,
                "uncertainty": 1.0,
                "method": "no_policy",
                "needs_gemini": True,
            }

        # Prefer numpy model (no PyTorch overhead)
        if self._numpy_model._loaded:
            action_id, confidence, uncertainty = self._numpy_model.predict(feature_vector)
        elif self._torch_model is not None:
            tensor = torch.from_numpy(feature_vector).float()
            action_id, confidence, uncertainty = self._torch_model.predict(tensor)
        else:
            return {
                "action": "stop",
                "action_id": 4,
                "confidence": 0.0,
                "uncertainty": 1.0,
                "method": "no_policy",
                "needs_gemini": True,
            }

        needs_gemini = uncertainty > self.uncertainty_threshold

        # Track intervention stats
        if needs_gemini:
            self.stats.record_gemini_intervention()
        else:
            self.stats.record_policy_decision()

        return {
            "action": ACTION_NAMES[action_id],
            "action_id": action_id,
            "confidence": round(confidence, 3),
            "uncertainty": round(uncertainty, 3),
            "method": "policy",
            "needs_gemini": needs_gemini,
        }

    def save_torch(self, model: Any) -> str:
        """Save a trained PyTorch model + export numpy weights."""
        torch_path = self.model_dir / "driver_policy.pt"
        numpy_path = self.model_dir / "driver_policy.npz"

        # Save PyTorch checkpoint
        torch.save(model.state_dict(), str(torch_path))

        # Export numpy weights for fast inference
        state = model.state_dict()
        np_data = {}
        backbone_layers = []
        for key in state:
            if key.startswith("backbone."):
                parts = key.split(".")
                idx = int(parts[1])
                if key.endswith(".weight"):
                    backbone_layers.append(("w", idx // 3, state[key].cpu().numpy()))
                elif key.endswith(".bias"):
                    backbone_layers.append(("b", idx // 3, state[key].cpu().numpy()))

        # Group by layer
        num_layers = max(l[1] for l in backbone_layers) + 1 if backbone_layers else 0
        np_data["num_layers"] = np.array(num_layers)
        for kind, idx, arr in backbone_layers:
            np_data[f"{'w' if kind == 'w' else 'b'}{idx}"] = arr

        np_data["action_w"] = state["action_head.weight"].cpu().numpy()
        np_data["action_b"] = state["action_head.bias"].cpu().numpy()
        np_data["unc_w"] = state["uncertainty_head.weight"].cpu().numpy()
        np_data["unc_b"] = state["uncertainty_head.bias"].cpu().numpy()

        np.savez_compressed(str(numpy_path), **np_data)

        logger.info(f"🧠 Driver policy saved: {torch_path} + {numpy_path}")
        self._torch_model = model
        self._numpy_model.load(str(numpy_path))
        self._available = True
        return str(torch_path)

    def get_stats(self) -> Dict[str, Any]:
        """Get policy stats + intervention tracking."""
        return {
            "available": self._available,
            "uncertainty_threshold": self.uncertainty_threshold,
            "model_dir": str(self.model_dir),
            "intervention": self.stats.to_dict(),
        }


# ── Intervention log (persistent, for graphing over time) ────────────

def log_intervention_stats(stats: InterventionStats):
    """Append intervention stats to a JSONL file for long-term tracking."""
    log_dir = Path.home() / ".repryntt" / "data" / "driver_evolution"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{time.strftime('%Y-%m-%d')}.jsonl"

    entry = {
        "ts": time.time(),
        **stats.to_dict(),
    }
    try:
        with open(log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ── Singleton ─────────────────────────────────────────────────────────

_policy: Optional[DriverPolicy] = None


def get_driver_policy() -> DriverPolicy:
    """Get or create the singleton driver policy."""
    global _policy
    if _policy is None:
        _policy = DriverPolicy()
    return _policy
