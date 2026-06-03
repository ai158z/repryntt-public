"""
repryntt.core.evolution.nav_rl_trainer — Tank Sim RL Training Entrypoint.

Runs TankQLearner on TankSimEnv (CPU, ~30s) then converts the learned Q-table
into synthetic (feature_vector, action) pairs and passes them to quick_train().
This gives the MLP pre-training before it sees any real hardware data.

Used by evolution_loop.py during the 3-5 AM overnight window.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np

logger = logging.getLogger(__name__)

# Observation layout from TankSimEnv (11 dims):
# [0:8]  ray distances (0-1 normalized)
# [8]    heading (0-1)
# [9]    distance to goal (0-1)
# [10]   angle to goal (-1 to 1)
OBS_DIMS = 11
FEATURE_DIM = 50
NUM_BINS = 4  # digitize uses bins [1,2,3,4], so max bin = 4


def _qtable_to_synthetic_dataset(q_table: dict) -> tuple[np.ndarray, np.ndarray]:
    """Convert a Q-table to synthetic (X, y) training data for the MLP.

    Each Q-table key is a discretized obs tuple. We un-bin back to a
    continuous approximation, then embed into the 50-dim DriverMLP feature space.
    """
    features = []
    labels = []

    for state_key, q_vals in q_table.items():
        best_action = int(np.argmax(q_vals))
        # Skip states where the agent just stopped (not useful training signal)
        if best_action == 4 and q_vals[4] == 0.0:
            continue

        obs_approx = np.array(state_key, dtype=np.float32) / NUM_BINS

        # Remap angle-to-goal slot [10] from [0,1] back to [-1,1]
        obs_approx_full = obs_approx.copy()
        if len(obs_approx_full) > 10:
            obs_approx_full[10] = obs_approx_full[10] * 2.0 - 1.0

        # Build 50-dim feature vector
        vec = np.zeros(FEATURE_DIM, dtype=np.float32)

        # Slots 0-2: obstacle density (front/center/right rays as proxy)
        obs_len = min(len(obs_approx_full), OBS_DIMS)
        ray_count = min(8, obs_len)
        if ray_count >= 3:
            vec[0] = 1.0 - obs_approx_full[2]  # left ray → left obstacle density
            vec[1] = 1.0 - obs_approx_full[0]  # front ray → center obstacle density
            vec[2] = 1.0 - obs_approx_full[6]  # right ray → right obstacle density

        # Slots 28-30: depth proxy (same as slots 0-2, mirroring DA2 depth layout)
        vec[28] = vec[0]
        vec[29] = vec[1]
        vec[30] = vec[2]

        # Slot 31: was-forward hint (strong forward Q-value → open path ahead)
        if obs_approx_full[0] > 0.6:  # front ray > 60% clear
            vec[31] = 1.0
        elif best_action in (2, 3):  # turn
            vec[32] = 1.0
        elif best_action == 1:  # backward
            vec[33] = 1.0

        features.append(vec)
        labels.append(best_action)

    X = np.array(features, dtype=np.float32)
    y = np.array(labels, dtype=np.int64)
    return X, y


def run_nav_rl_training(episodes: int = 300) -> Dict[str, Any]:
    """Run sim RL training and convert to synthetic MLP pre-training data.

    Returns result dict. Key 'error' present on failure.
    """
    try:
        from repryntt.hardware.tank_sim import TankSimEnv, TankQLearner, GYM_AVAILABLE
        from repryntt.hardware.driver_trainer import load_experience, MIN_SAMPLES
    except ImportError as e:
        return {"error": f"tank_sim import failed: {e}"}

    if not GYM_AVAILABLE:
        return {"error": "gymnasium not installed — skipping sim RL training"}

    # Skip sim RL once we have enough real hardware data — the real model
    # is always better than a synthetic Q-table approximation.
    real_samples = len(load_experience())
    if real_samples >= MIN_SAMPLES:
        return {"error": f"sim RL skipped: {real_samples} real samples already exist (>= {MIN_SAMPLES})"}

    try:
        env = TankSimEnv()
        learner = TankQLearner(epsilon_decay=0.99, epsilon_min=0.05)
        logger.info(f"🤖 Nav sim RL: running {episodes} episodes on TankSimEnv")
        rl_result = learner.train(env, episodes=episodes, verbose=False)
    except Exception as e:
        return {"error": f"sim RL training failed: {e}"}

    q_table = learner.q_table
    if not q_table:
        return {"error": "Q-table empty after training"}

    logger.info(f"🤖 Nav sim RL: goal_rate={rl_result.get('goal_rate', 0):.1%}, "
                f"q_states={len(q_table)}")

    # Convert Q-table to synthetic MLP training data
    X, y = _qtable_to_synthetic_dataset(q_table)
    if len(X) < 5:
        return {"error": f"too few synthetic samples from Q-table: {len(X)}"}

    logger.info(f"🤖 Nav sim RL: {len(X)} synthetic samples → MLP quick_train")

    try:
        from repryntt.hardware.driver_trainer import train_policy, MIN_SAMPLES
        # Override MIN_SAMPLES check: synthetic data is always valid
        mlp_result = train_policy(X, y, epochs=80)
    except Exception as e:
        return {"error": f"MLP training on synthetic data failed: {e}"}

    return {
        **rl_result,
        "synthetic_samples": len(X),
        "mlp_train_acc": mlp_result.get("overall_acc"),
        "mlp_val_acc": mlp_result.get("best_val_acc"),
        "model_path": mlp_result.get("model_path"),
    }


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import json
    result = run_nav_rl_training(episodes=300)
    print(json.dumps(result, indent=2))
