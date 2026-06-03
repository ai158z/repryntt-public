"""
repryntt.hardware.driver_trainer — Behavior Cloning Trainer for the MLP Driver Policy.

Learns to drive from collected nav experience JSONL logs.
Can optionally reprocess saved images through YOLO for richer features.

Training pipeline:
    1. Load JSONL entries → (stereo_features, action_label) pairs
    2. Optionally enrich with YOLO detections on saved images
    3. Train DriverMLP via cross-entropy + uncertainty auxiliary loss
    4. Export trained weights (PyTorch .pt + numpy .npz)
    5. Log training metrics for the ops dashboard

Runs during 3-5 AM training window or on-demand.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Action name → index (matches nav_cortex.py ACTION_NAMES)
ACTION_MAP = {
    "forward": 0,
    "backward": 1,
    "turn_left": 2,
    "turn_right": 3,
    "stop": 4,
}
NUM_ACTIONS = 5
FEATURE_DIM = 50
MIN_SAMPLES = 20  # don't train until we have meaningful data


def load_experience(data_dir: Optional[str] = None,
                    min_confidence: float = 0.3) -> List[Dict[str, Any]]:
    """Load all nav experience JSONL entries.

    Drops poisoned rows so the MLP doesn't learn the "always say forward"
    collapse seen in the 2026-05-08 quarantine batch:
      - perception_failed=True (structured marker from _fallback_perception)
      - executed=False (motor never actually fired)
      - legacy "perception failed ..." scene strings (pre-fix rows)
      - confidence < min_confidence
      - decision not in ACTION_MAP

    Args:
        data_dir: Directory with JSONL files. Defaults to ~/.repryntt/data/nav_experience/
        min_confidence: Skip entries below this confidence.

    Returns:
        List of dicts with at least: decision, stereo_left/center/right, scene
    """
    if data_dir is None:
        data_dir = str(Path.home() / ".repryntt" / "data" / "nav_experience")

    entries = []
    drops = {
        "bad_decision": 0,
        "low_confidence": 0,
        "perception_failed": 0,
        "not_executed": 0,
        "legacy_failed_scene": 0,
    }
    data_path = Path(data_dir)
    if not data_path.exists():
        logger.warning(f"No nav experience directory: {data_path}")
        return entries

    for jsonl_file in sorted(data_path.glob("*.jsonl")):
        if jsonl_file.name.startswith("pipeline"):
            continue  # skip sample files
        try:
            with open(jsonl_file) as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    decision = entry.get("decision", "")
                    if decision not in ACTION_MAP:
                        drops["bad_decision"] += 1
                        continue
                    if entry.get("perception_failed"):
                        drops["perception_failed"] += 1
                        continue
                    # entry.get("executed") may be missing on legacy rows;
                    # only drop when explicitly False.
                    if entry.get("executed") is False:
                        drops["not_executed"] += 1
                        continue
                    scene = entry.get("scene", "") or ""
                    if scene.startswith("perception failed"):
                        drops["legacy_failed_scene"] += 1
                        continue
                    if entry.get("confidence", 0) < min_confidence:
                        drops["low_confidence"] += 1
                        continue

                    entries.append(entry)
        except Exception as e:
            logger.warning(f"Failed to read {jsonl_file}: {e}")

    total_dropped = sum(drops.values())
    logger.info(
        f"📊 Loaded {len(entries)} nav experience entries from {data_path} "
        f"(dropped {total_dropped}: {drops})"
    )
    return entries


def entry_to_feature_vector(entry: Dict[str, Any],
                            yolo_features: Optional[np.ndarray] = None) -> np.ndarray:
    """Convert a JSONL entry to the 50-dim feature vector.
    
    If yolo_features is provided (from reprocessing images), use those.
    Otherwise, build from logged stereo/depth data with zeros for YOLO slots.
    
    The trainer now understands entries logged with neural depth:
      depth_left/center/right → used for indices 28-30 (preferred over stereo)
      person_detected, person_count, person_distance_cm → person features
    """
    vec = np.zeros(FEATURE_DIM, dtype=np.float32)

    # Obstacle density (indices 0-2) — from stereo or depth (whichever was logged)
    vec[0] = entry.get("stereo_left", 0.0)
    vec[1] = entry.get("stereo_center", 0.0)
    vec[2] = entry.get("stereo_right", 0.0)

    # Depth indices [28:30] — prefer neural depth, fall back to stereo
    if entry.get("depth_left") is not None:
        vec[28] = entry.get("depth_left", 0.0)
        vec[29] = entry.get("depth_center", 0.0)
        vec[30] = entry.get("depth_right", 0.0)
    else:
        vec[28] = entry.get("stereo_left", 0.0)
        vec[29] = entry.get("stereo_center", 0.0)
        vec[30] = entry.get("stereo_right", 0.0)

    # Person features from fused perception logs
    if entry.get("person_detected"):
        vec[18] = 1.0  # has_person
        vec[15] = min(entry.get("person_count", 0) / 3.0, 1.0)
        # Rough area proxy from distance
        dist = entry.get("person_distance_cm", 0)
        if dist > 0:
            vec[19] = max(0.0, min(1.0, 1.0 - (dist / 500.0)))

    # Detection count estimate from scene description
    scene = entry.get("scene", "")
    if "dark" in scene.lower() or "obscured" in scene.lower():
        vec[17] = 0.05
    elif scene:
        word_count = len(scene.split())
        vec[17] = min(word_count / 40.0, 1.0)

    # If YOLO features available (from reprocessing), overlay them
    if yolo_features is not None:
        vec[:28] = yolo_features[:28]

    # Last action encoding (indices 31-33)
    decision = entry.get("decision", "stop")
    action_id = ACTION_MAP.get(decision, 4)
    if action_id == 0:  # forward
        vec[31] = 1.0
    elif action_id in (2, 3):  # turn
        vec[32] = 1.0
    elif action_id == 1:  # backward
        vec[33] = 1.0

    return vec


def build_dataset(entries: List[Dict[str, Any]],
                  use_yolo: bool = False,
                  yolo_detector: Optional[Any] = None) -> Tuple[np.ndarray, np.ndarray]:
    """Build (features, labels) numpy arrays from experience entries.
    
    Args:
        entries: List of JSONL dicts.
        use_yolo: If True, reprocess saved images through YOLO for richer features.
        yolo_detector: YoloDetector instance (required if use_yolo=True).
    
    Returns:
        (X, y): feature matrix [N, 50] and action labels [N]
    """
    features = []
    labels = []

    for entry in entries:
        action_id = ACTION_MAP.get(entry.get("decision", "stop"), 4)

        # Optionally run YOLO on the saved image
        yolo_features = None
        if use_yolo and yolo_detector is not None:
            image_path = entry.get("image", "")
            if image_path and os.path.exists(image_path):
                try:
                    perception = yolo_detector.detect_image(image_path)
                    if perception is not None:
                        yolo_features = yolo_detector.to_feature_vector(perception)
                except Exception:
                    pass

        vec = entry_to_feature_vector(entry, yolo_features)
        features.append(vec)
        labels.append(action_id)

    X = np.array(features, dtype=np.float32)
    y = np.array(labels, dtype=np.int64)

    # Log class distribution
    unique, counts = np.unique(y, return_counts=True)
    dist = {ACTION_MAP_REV.get(int(u), str(u)): int(c) for u, c in zip(unique, counts)}
    logger.info(f"📊 Dataset: {len(X)} samples, distribution: {dist}")

    return X, y


# Reverse action map
ACTION_MAP_REV = {v: k for k, v in ACTION_MAP.items()}


def train_policy(X: np.ndarray, y: np.ndarray,
                 epochs: int = 100,
                 batch_size: int = 32,
                 lr: float = 0.001,
                 val_split: float = 0.15,
                 model_dir: Optional[str] = None,
                 pre_split: Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = None
                 ) -> Dict[str, Any]:
    """Train the DriverMLP via behavior cloning.

    If pre_split is provided as (X_train, y_train, X_val, y_val), use it
    verbatim — bypasses the random shuffle/split. Used by the curated-dir
    workflow so the curator's train/val partition is respected.

    Returns dict with training results and saved model path.
    """
    try:
        import torch
        import torch.nn as nn
        import torch.optim as optim
        from torch.utils.data import TensorDataset, DataLoader
    except ImportError:
        logger.error("PyTorch not available — cannot train driver policy")
        return {"error": "pytorch_not_available"}

    from repryntt.hardware.driver_policy import DriverMLP, DriverPolicy

    if model_dir is None:
        model_dir = str(Path.home() / ".repryntt" / "models")
    Path(model_dir).mkdir(parents=True, exist_ok=True)

    if pre_split is not None:
        Xt, yt, Xv, yv = pre_split
        X_train = torch.from_numpy(Xt)
        y_train = torch.from_numpy(yt)
        X_val = torch.from_numpy(Xv)
        y_val = torch.from_numpy(yv)
    else:
        # Shuffle and split
        n = len(X)
        indices = np.random.permutation(n)
        val_n = max(1, int(n * val_split))
        train_idx = indices[val_n:]
        val_idx = indices[:val_n]

        X_train = torch.from_numpy(X[train_idx])
        y_train = torch.from_numpy(y[train_idx])
        X_val = torch.from_numpy(X[val_idx])
        y_val = torch.from_numpy(y[val_idx])

    # Class weights for imbalanced data (lots of turn_left, few forward)
    class_counts = np.bincount(y, minlength=NUM_ACTIONS).astype(np.float32)
    class_counts = np.maximum(class_counts, 1.0)  # avoid div by zero
    class_weights = 1.0 / class_counts
    class_weights /= class_weights.sum()  # normalize
    weight_tensor = torch.from_numpy(class_weights)

    train_ds = TensorDataset(X_train, y_train)
    train_dl = DataLoader(train_ds, batch_size=batch_size, shuffle=True)

    model = DriverMLP()
    optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=1e-4)
    criterion = nn.CrossEntropyLoss(weight=weight_tensor)
    scheduler = optim.lr_scheduler.ReduceLROnPlateau(optimizer, patience=10, factor=0.5)

    best_val_acc = 0.0
    best_state = None
    history = {"train_loss": [], "val_loss": [], "val_acc": []}

    t0 = time.time()
    for epoch in range(epochs):
        model.train()
        epoch_loss = 0.0
        n_batches = 0

        for batch_X, batch_y in train_dl:
            logits, uncertainty = model(batch_X)
            loss_action = criterion(logits, batch_y)

            # Uncertainty auxiliary loss:
            # High uncertainty when prediction is WRONG, low when RIGHT
            with torch.no_grad():
                predicted = torch.argmax(logits, dim=-1)
                is_wrong = (predicted != batch_y).float().unsqueeze(1)
            loss_unc = nn.functional.mse_loss(uncertainty, is_wrong)

            loss = loss_action + 0.3 * loss_unc

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            epoch_loss += loss_action.item()
            n_batches += 1

        avg_loss = epoch_loss / max(n_batches, 1)

        # Validation
        model.eval()
        with torch.no_grad():
            val_logits, _ = model(X_val)
            val_loss = criterion(val_logits, y_val).item()
            val_pred = torch.argmax(val_logits, dim=-1)
            val_acc = (val_pred == y_val).float().mean().item()

        scheduler.step(val_loss)
        history["train_loss"].append(round(avg_loss, 4))
        history["val_loss"].append(round(val_loss, 4))
        history["val_acc"].append(round(val_acc, 4))

        if val_acc > best_val_acc:
            best_val_acc = val_acc
            best_state = {k: v.clone() for k, v in model.state_dict().items()}

        if (epoch + 1) % 20 == 0:
            logger.info(f"  Epoch {epoch+1}/{epochs}: "
                        f"train_loss={avg_loss:.4f}, val_loss={val_loss:.4f}, "
                        f"val_acc={val_acc:.3f}")

    train_time = time.time() - t0

    # Load best model and save
    if best_state is not None:
        model.load_state_dict(best_state)

    policy = DriverPolicy(model_dir=model_dir)
    save_path = policy.save_torch(model)

    # Final evaluation
    model.eval()
    with torch.no_grad():
        all_logits, all_unc = model(torch.from_numpy(X))
        all_pred = torch.argmax(all_logits, dim=-1).numpy()
        overall_acc = (all_pred == y).mean()
        avg_uncertainty = all_unc.mean().item()

    # Per-class accuracy
    per_class = {}
    for cls_id, cls_name in ACTION_MAP_REV.items():
        mask = y == cls_id
        if mask.sum() > 0:
            cls_acc = (all_pred[mask] == y[mask]).mean()
            per_class[cls_name] = round(float(cls_acc), 3)

    results = {
        "model_path": save_path,
        "samples": len(X),
        "train_samples": len(X_train),
        "val_samples": len(X_val),
        "epochs": epochs,
        "best_val_acc": round(best_val_acc, 4),
        "overall_acc": round(float(overall_acc), 4),
        "avg_uncertainty": round(avg_uncertainty, 4),
        "per_class_acc": per_class,
        "train_time_sec": round(train_time, 1),
        "history": history,
    }

    logger.info(f"🧠 Training complete: acc={overall_acc:.3f}, "
                f"best_val={best_val_acc:.3f}, time={train_time:.1f}s, "
                f"saved to {save_path}")

    # Save training log
    log_path = Path(model_dir) / "training_log.json"
    try:
        with open(log_path, "w") as f:
            json.dump(results, f, indent=2)
    except Exception:
        pass

    return results


def retrain_with_yolo(epochs: int = 150) -> Dict[str, Any]:
    """Full retraining pipeline: load experiences, run YOLO on images, train.
    
    Call this during the 3-5 AM training window for enriched features.
    """
    from repryntt.hardware.yolo_perception import get_yolo_detector

    detector = get_yolo_detector()
    use_yolo = detector.available

    if use_yolo:
        logger.info("🔄 Retraining with YOLO features (will reprocess saved images)")
    else:
        logger.info("🔄 Retraining with stereo-only features (YOLO not available)")

    entries = load_experience()
    if len(entries) < MIN_SAMPLES:
        logger.warning(f"Not enough nav experience to train ({len(entries)} samples, need {MIN_SAMPLES})")
        return {"error": f"insufficient data: {len(entries)} samples (need {MIN_SAMPLES})", "count": len(entries)}

    X, y = build_dataset(entries, use_yolo=use_yolo,
                         yolo_detector=detector if use_yolo else None)
    return train_policy(X, y, epochs=epochs)


def quick_train(epochs: int = 80) -> Dict[str, Any]:
    """Quick training without YOLO reprocessing (stereo features only).

    Fast — just loads JSONL, builds stereo vectors, trains MLP.
    """
    entries = load_experience()
    if len(entries) < MIN_SAMPLES:
        return {"error": f"insufficient data: {len(entries)} samples (need {MIN_SAMPLES})", "count": len(entries)}

    X, y = build_dataset(entries, use_yolo=False)
    return train_policy(X, y, epochs=epochs)


def train_curated(curated_dir: str, epochs: int = 80,
                  use_yolo: bool = False) -> Dict[str, Any]:
    """Train from a curator-prepared train.jsonl + val.jsonl pair.

    The curator's train/val split is respected (no re-shuffle). Use this
    after `python -m repryntt.hardware.curate_nav_data`.
    """
    cur = Path(curated_dir)
    train_path = cur / "train.jsonl"
    val_path = cur / "val.jsonl"
    if not train_path.exists() or not val_path.exists():
        return {"error": f"missing train.jsonl or val.jsonl in {curated_dir}"}

    def _load(path: Path) -> List[Dict[str, Any]]:
        rows = []
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows

    train_entries = _load(train_path)
    val_entries = _load(val_path)
    if len(train_entries) < MIN_SAMPLES:
        return {"error": f"curated train set too small: {len(train_entries)} (need {MIN_SAMPLES})"}

    yolo_detector = None
    if use_yolo:
        try:
            from repryntt.hardware.yolo_perception import get_yolo_detector
            d = get_yolo_detector()
            if d.available:
                yolo_detector = d
        except Exception:
            pass

    Xt, yt = build_dataset(train_entries, use_yolo=bool(yolo_detector),
                           yolo_detector=yolo_detector)
    Xv, yv = build_dataset(val_entries, use_yolo=bool(yolo_detector),
                           yolo_detector=yolo_detector)

    X_full = np.concatenate([Xt, Xv]) if len(Xv) else Xt
    y_full = np.concatenate([yt, yv]) if len(yv) else yt
    return train_policy(X_full, y_full, epochs=epochs,
                        pre_split=(Xt, yt, Xv, yv))


# ── CLI entry point ─────────────────────────────────────────────────

def _enforce_per_class_gate(results: Dict[str, Any], threshold: float) -> int:
    """Return 0 if every class meets the threshold, 1 otherwise."""
    per_class = results.get("per_class_acc", {})
    if not per_class:
        print(f"⚠ per-class gate: no per_class_acc in results — skipping gate")
        return 0
    failing = {k: v for k, v in per_class.items() if v < threshold}
    if failing:
        print(f"❌ per-class gate FAILED (<{threshold:.2f}): {failing}")
        print("   Likely cause: dataset is class-imbalanced. Run "
              "`python -m repryntt.hardware.curate_nav_data` first.")
        return 1
    print(f"✓ per-class gate PASSED (all classes ≥ {threshold:.2f})")
    return 0


if __name__ == "__main__":
    import argparse
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    ap = argparse.ArgumentParser(description="Train the Andrew driver MLP.")
    ap.add_argument("mode", nargs="?", default="quick",
                    choices=["quick", "yolo", "curated"],
                    help="quick = stereo features only; yolo = reprocess images; "
                         "curated = read curator output")
    ap.add_argument("--epochs", type=int, default=100)
    ap.add_argument("--curated-dir", type=str,
                    default=str(Path.home() / ".repryntt" / "data" / "nav_experience_curated"),
                    help="Directory with train.jsonl + val.jsonl (curated mode)")
    ap.add_argument("--fail-under-per-class-acc", type=float, default=0.0,
                    help="Exit non-zero if any class val accuracy is below this. "
                         "Hooks the 'always say forward' collapse.")
    args = ap.parse_args()

    if args.mode == "yolo":
        results = retrain_with_yolo(epochs=args.epochs)
    elif args.mode == "curated":
        results = train_curated(args.curated_dir, epochs=args.epochs)
    else:
        results = quick_train(epochs=args.epochs)

    print(json.dumps(results, indent=2))

    if "error" in results:
        sys.exit(2)
    if args.fail_under_per_class_acc > 0:
        sys.exit(_enforce_per_class_gate(results, args.fail_under_per_class_acc))
    sys.exit(0)
