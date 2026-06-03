"""
repryntt.hardware.optical_flow — Lightweight motion detection between VLM calls.

Fills the speed gap between:
  • Stereo + YOLO (30 FPS, local, reflex layer)
  • VLM (0.5–1 Hz, cloud, reasoning layer)

Runs on OpenCV Farneback dense optical flow at ~20–50ms/frame on the Jetson
Orin Nano. No API calls, no GPU model loads — just numpy on grayscale frames.

Use cases:
  1. ``has_significant_motion(img_a, img_b)`` → bool
     Detect "something moved in the scene" (person walked in, pet darted,
     door opened). Caller can trigger an immediate VLM tick instead of
     waiting for the next scheduled one.

  2. ``estimate_ego_motion(img_a, img_b)`` → dict
     Rough "am I moving forward / turning" estimate from flow field.
     Used for dead-reckoning between motor commands.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

try:
    import cv2  # noqa: F401
    _CV2_OK = True
except Exception as _e:
    logger.warning(f"OpenCV unavailable — optical flow disabled: {_e}")
    _CV2_OK = False


# ── Tunables ─────────────────────────────────────────────────────────
# Downscale factor: flow on 320x180 is plenty for motion detection and
# ~4x faster than 1280x720. Navigation doesn't need pixel-perfect flow.
DOWNSCALE_WIDTH = 320

# Magnitude threshold (pixels) — flow vectors below this count as noise
FLOW_NOISE_FLOOR = 1.5

# Fraction of pixels above the noise floor to call "significant motion"
MOTION_PIXEL_RATIO = 0.08   # 8% of the frame in motion = something happened

# Ego-motion heuristics — the robot itself moving registers flow too.
# Forward motion: flow points radially outward from center (expanding field)
# Turn: most flow is horizontal, same direction across the frame
EGO_CENTER_RADIUS_FRAC = 0.15  # center quarter of frame is "FOE" region


@dataclass
class FlowResult:
    mean_magnitude: float = 0.0      # avg flow magnitude (px)
    motion_pixel_ratio: float = 0.0  # fraction of pixels with magnitude > floor
    significant: bool = False        # True if motion_pixel_ratio > threshold
    dominant_dx: float = 0.0         # mean horizontal flow (+ = right)
    dominant_dy: float = 0.0         # mean vertical flow (+ = down)
    elapsed_ms: float = 0.0
    error: Optional[str] = None

    def summary(self) -> str:
        if self.error:
            return f"flow_error:{self.error}"
        if self.significant:
            return (f"MOTION mag={self.mean_magnitude:.2f}px "
                    f"ratio={self.motion_pixel_ratio:.1%} "
                    f"dx={self.dominant_dx:+.2f} dy={self.dominant_dy:+.2f} "
                    f"({self.elapsed_ms:.0f}ms)")
        return (f"quiet mag={self.mean_magnitude:.2f}px "
                f"ratio={self.motion_pixel_ratio:.1%} "
                f"({self.elapsed_ms:.0f}ms)")


class OpticalFlowDetector:
    """Singleton motion detector. Caches the last grayscale frame.

    Call ``.check(image_path)`` each cycle with a fresh camera frame.
    Returns a FlowResult describing scene change vs. the prior call.
    The first call always returns ``significant=False`` (no reference).
    """

    def __init__(self):
        self._prev_gray: Optional[np.ndarray] = None
        self._prev_ts: float = 0.0

    def reset(self):
        """Clear the cached frame so the next check starts fresh."""
        self._prev_gray = None
        self._prev_ts = 0.0

    def check(self, image_path: str) -> FlowResult:
        """Compute flow vs. the previously cached frame."""
        if not _CV2_OK:
            return FlowResult(error="no_cv2")

        t0 = time.time()
        try:
            import cv2
            frame = cv2.imread(image_path)
            if frame is None or frame.size == 0:
                return FlowResult(error="read_failed")

            # Downscale for speed — keep aspect ratio
            h, w = frame.shape[:2]
            scale = DOWNSCALE_WIDTH / float(w) if w > DOWNSCALE_WIDTH else 1.0
            if scale < 1.0:
                frame = cv2.resize(
                    frame,
                    (DOWNSCALE_WIDTH, int(h * scale)),
                    interpolation=cv2.INTER_AREA,
                )

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

            if self._prev_gray is None or self._prev_gray.shape != gray.shape:
                self._prev_gray = gray
                self._prev_ts = time.time()
                return FlowResult(elapsed_ms=(time.time() - t0) * 1000.0)

            # Farneback dense flow — fastest OpenCV option
            flow = cv2.calcOpticalFlowFarneback(
                self._prev_gray, gray, None,
                pyr_scale=0.5, levels=2, winsize=15,
                iterations=2, poly_n=5, poly_sigma=1.1, flags=0,
            )

            fx, fy = flow[..., 0], flow[..., 1]
            mag = np.sqrt(fx * fx + fy * fy)
            mean_mag = float(mag.mean())
            motion_mask = mag > FLOW_NOISE_FLOOR
            ratio = float(motion_mask.mean())
            dx = float(fx[motion_mask].mean()) if motion_mask.any() else 0.0
            dy = float(fy[motion_mask].mean()) if motion_mask.any() else 0.0

            significant = ratio >= MOTION_PIXEL_RATIO

            # Roll the buffer
            self._prev_gray = gray
            self._prev_ts = time.time()

            return FlowResult(
                mean_magnitude=mean_mag,
                motion_pixel_ratio=ratio,
                significant=significant,
                dominant_dx=dx,
                dominant_dy=dy,
                elapsed_ms=(time.time() - t0) * 1000.0,
            )
        except Exception as e:
            return FlowResult(error=str(e), elapsed_ms=(time.time() - t0) * 1000.0)


_singleton: Optional[OpticalFlowDetector] = None


def get_optical_flow() -> OpticalFlowDetector:
    """Process-wide optical flow detector."""
    global _singleton
    if _singleton is None:
        _singleton = OpticalFlowDetector()
    return _singleton


__all__ = ["OpticalFlowDetector", "FlowResult", "get_optical_flow"]
