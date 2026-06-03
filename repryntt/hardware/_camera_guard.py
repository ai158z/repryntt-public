"""
repryntt.hardware._camera_guard — refuse direct camera opens once broker is live.

Why: see the project memo. Multiple call sites used to open /dev/video0
independently. The broker is now the single legitimate opener. This guard
wraps cv2.VideoCapture so that any attempt to open a CSI device (integer
0/1, or "/dev/videoN") from inside the daemon raises loudly instead of
silently double-opening the camera and corrupting frames.

Bypass for the broker's own producer thread:
    REPRYNTT_ALLOW_DIRECT_CAMERA=1
or by calling the saved original via repryntt.hardware.camera_broker.

This guard is opt-in — install_guard() must be called explicitly. The
broker installs it on first sensor start.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

try:
    import cv2
    _ORIGINAL_VIDEO_CAPTURE = cv2.VideoCapture
    CV2_AVAILABLE = True
except ImportError:
    cv2 = None  # type: ignore
    _ORIGINAL_VIDEO_CAPTURE = None
    CV2_AVAILABLE = False

_INSTALLED = False
_DIRECT_CSI_DEVICE_PATHS = ("/dev/video0", "/dev/video1")


def _is_csi_device_arg(arg: Any) -> bool:
    """Return True if `arg` is the index/path of a CSI camera we manage."""
    if isinstance(arg, int):
        return arg in (0, 1)
    if isinstance(arg, str):
        return arg in _DIRECT_CSI_DEVICE_PATHS
    return False


def _is_gstreamer_pipeline(arg: Any) -> bool:
    """A long string with gstreamer plugin names — let it through."""
    return isinstance(arg, str) and (
        "nvarguscamerasrc" in arg
        or "v4l2src" in arg
        or "appsink" in arg
    )


def install_guard() -> None:
    """Wrap cv2.VideoCapture so direct CSI opens raise.

    Safe to call multiple times.
    """
    global _INSTALLED
    if _INSTALLED or not CV2_AVAILABLE:
        return

    original = _ORIGINAL_VIDEO_CAPTURE

    def guarded_video_capture(*args, **kwargs):
        if os.environ.get("REPRYNTT_ALLOW_DIRECT_CAMERA") == "1":
            return original(*args, **kwargs)
        first = args[0] if args else None
        # GStreamer pipeline strings (the broker's own path) are fine.
        if _is_gstreamer_pipeline(first):
            return original(*args, **kwargs)
        if _is_csi_device_arg(first):
            raise RuntimeError(
                "Direct camera open refused: use repryntt.hardware.camera_broker. "
                "The broker is the only legitimate opener of /dev/video0 and "
                "/dev/video1. If you really need direct access (for a one-off "
                "diagnostic), set REPRYNTT_ALLOW_DIRECT_CAMERA=1."
            )
        return original(*args, **kwargs)

    cv2.VideoCapture = guarded_video_capture  # type: ignore[assignment]
    _INSTALLED = True
    logger.info("camera_broker: direct-cv2 camera guard installed")


def uninstall_guard() -> None:
    """Restore the original cv2.VideoCapture (for tests)."""
    global _INSTALLED
    if not _INSTALLED or not CV2_AVAILABLE or _ORIGINAL_VIDEO_CAPTURE is None:
        return
    cv2.VideoCapture = _ORIGINAL_VIDEO_CAPTURE  # type: ignore[assignment]
    _INSTALLED = False


__all__ = ["install_guard", "uninstall_guard"]
