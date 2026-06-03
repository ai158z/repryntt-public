"""
repryntt.hardware.camera — cross-platform camera discovery and capture.

Works on Linux (USB + CSI/Jetson), macOS (USB + built-in), and Windows (USB + built-in).
Uses OpenCV as the primary backend with Jetson GStreamer as a specialized fast path.

Usage:
    from repryntt.hardware.camera import discover_cameras, capture_frame, get_camera

    cameras = discover_cameras()     # [{index: 0, name: "USB HD Webcam", ...}, ...]
    frame = capture_frame(0)         # numpy array (BGR) or None
    cam = get_camera(0)              # CameraHandle for streaming
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("repryntt.hardware.camera")

# ── Camera info dataclass ────────────────────────────────────────────────


@dataclass
class CameraInfo:
    """Discovered camera metadata."""
    index: int
    name: str
    backend: str          # "opencv", "gstreamer", "v4l2", "avfoundation", "dshow"
    width: int = 0
    height: int = 0
    fps: float = 0.0
    is_csi: bool = False  # True for Jetson CSI cameras
    device_path: str = "" # e.g. "/dev/video0" on Linux


# ── Discovery ────────────────────────────────────────────────────────────

_camera_cache: Optional[List[CameraInfo]] = None


def discover_cameras(*, max_index: int = 10, force_refresh: bool = False) -> List[CameraInfo]:
    """Probe for available cameras on this system.

    Tries indices 0..max_index using OpenCV. On Linux, also checks /dev/videoN.
    On Jetson, detects CSI cameras via nvarguscamerasrc.

    Returns a list of CameraInfo for all cameras that successfully open+read.
    Results are cached after first call (use force_refresh=True to re-probe).
    """
    global _camera_cache
    if _camera_cache is not None and not force_refresh:
        return list(_camera_cache)

    cameras: List[CameraInfo] = []

    try:
        import cv2
    except ImportError:
        logger.warning("OpenCV not installed — cannot discover cameras")
        _camera_cache = []
        return []

    # Strategy 1: Jetson CSI cameras (Linux + nvarguscamerasrc)
    if sys.platform.startswith("linux"):
        cameras.extend(_probe_jetson_csi(cv2))

    # Strategy 2: Standard OpenCV enumeration (all platforms)
    # On Linux, also correlate with /dev/videoN
    csi_indices = {c.index for c in cameras}

    for idx in range(max_index):
        if idx in csi_indices:
            continue  # Already found as CSI

        try:
            cap = cv2.VideoCapture(idx)
            if not cap.isOpened():
                continue

            ret, frame = cap.read()
            if not ret:
                cap.release()
                continue

            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
            backend_name = cap.getBackendName() if hasattr(cap, 'getBackendName') else "opencv"
            cap.release()

            # Try to get a friendly name
            name = _get_camera_name(idx)
            device_path = f"/dev/video{idx}" if sys.platform.startswith("linux") else ""

            cameras.append(CameraInfo(
                index=idx,
                name=name,
                backend=backend_name.lower() if isinstance(backend_name, str) else "opencv",
                width=w,
                height=h,
                fps=fps,
                device_path=device_path,
            ))
            logger.info(f"Found camera {idx}: {name} ({w}x{h} @ {fps:.0f}fps)")

        except Exception as e:
            logger.debug(f"Camera {idx} probe failed: {e}")
            continue

    _camera_cache = cameras
    logger.info(f"Camera discovery complete: {len(cameras)} camera(s) found")
    return list(cameras)


def _probe_jetson_csi(cv2) -> List[CameraInfo]:
    """Probe for Jetson CSI cameras using nvarguscamerasrc."""
    cameras = []

    # Check if this is actually a Jetson
    if not os.path.exists("/proc/device-tree/model"):
        return cameras
    try:
        model = Path("/proc/device-tree/model").read_text().strip("\x00").strip()
        if "jetson" not in model.lower():
            return cameras
    except Exception:
        return cameras

    for idx in range(4):  # Jetson supports up to 4 CSI cameras
        try:
            pipeline = (
                f"nvarguscamerasrc sensor-id={idx} num-buffers=1 ! "
                f"video/x-raw(memory:NVMM),width=640,height=480,framerate=30/1 ! "
                f"nvvidconv ! video/x-raw,format=BGRx ! videoconvert ! "
                f"video/x-raw,format=BGR ! appsink drop=1"
            )
            cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
            if cap.isOpened():
                ret, _ = cap.read()
                cap.release()
                if ret:
                    cameras.append(CameraInfo(
                        index=idx,
                        name=f"CSI Camera {idx} (IMX219)",
                        backend="gstreamer",
                        width=1280,
                        height=720,
                        fps=60.0,
                        is_csi=True,
                        device_path=f"/dev/video{idx}",
                    ))
                    logger.info(f"Found Jetson CSI camera at sensor-id={idx}")
            else:
                cap.release()
        except Exception:
            continue

    return cameras


def _get_camera_name(index: int) -> str:
    """Try to get a human-friendly camera name."""
    # Linux: read from /sys
    if sys.platform.startswith("linux"):
        try:
            name_path = f"/sys/class/video4linux/video{index}/name"
            if os.path.exists(name_path):
                return Path(name_path).read_text().strip()
        except Exception:
            pass

    # macOS: system_profiler could work but is slow
    # Windows: DirectShow device enumeration is complex
    # Fallback: generic name
    return f"Camera {index}"


# ── Single frame capture ─────────────────────────────────────────────────


def capture_frame(
    camera_index: int = 0,
    *,
    width: int = 1280,
    height: int = 720,
):
    """Capture a single frame from the specified camera.

    Returns a BGR numpy array, or None if capture fails.
    Automatically uses Jetson GStreamer for CSI cameras.
    """
    try:
        import cv2
    except ImportError:
        logger.error("OpenCV not installed")
        return None

    # Check if this is a known CSI camera
    known = discover_cameras()
    cam_info = next((c for c in known if c.index == camera_index), None)

    if cam_info and cam_info.is_csi:
        return _capture_jetson_csi(cv2, camera_index, width, height)

    # Standard OpenCV capture
    try:
        cap = cv2.VideoCapture(camera_index)
        if not cap.isOpened():
            logger.warning(f"Cannot open camera {camera_index}")
            return None

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

        # Warm up — auto-exposure / auto-white-balance need 10-15 frames
        # to converge. Too few warmup reads ⇒ overexposed first frames
        # under bright lighting.
        for _ in range(15):
            cap.read()

        ret, frame = cap.read()
        cap.release()

        if ret:
            return frame
        return None
    except Exception as e:
        logger.error(f"Capture failed on camera {camera_index}: {e}")
        return None


def _capture_jetson_csi(cv2, sensor_id: int, width: int, height: int):
    """Capture via Jetson GStreamer pipeline.

    IMX219 AE/AWB needs 10-30 frames to converge — without enough warmup
    the captured frame is over- or under-exposed. We also verify the
    final frame isn't saturated and re-read more frames if so.
    """
    pipeline = (
        f"nvarguscamerasrc sensor-id={sensor_id} num-buffers=60 ! "
        f"video/x-raw(memory:NVMM),width={width},height={height},framerate=30/1 ! "
        f"nvvidconv ! video/x-raw,format=BGRx ! videoconvert ! "
        f"video/x-raw,format=BGR ! appsink drop=1"
    )
    try:
        cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        if not cap.isOpened():
            return None
        # Drop ≥20 frames so AE/AWB converges before we keep one.
        frame = None
        for _ in range(25):
            ret, f = cap.read()
            if ret:
                frame = f
        # If still saturated, keep reading until we either get a usable
        # frame or run out of buffers.
        if frame is not None and frame.mean() >= 245:
            for _ in range(30):
                ret, f = cap.read()
                if ret:
                    frame = f
                    if frame.mean() < 245:
                        break
        cap.release()
        return frame
    except Exception:
        return None


# ── Camera handle for streaming ──────────────────────────────────────────


class CameraHandle:
    """Persistent camera connection for streaming use cases.

    Usage:
        cam = CameraHandle(0)
        cam.open()
        while True:
            frame = cam.read()
            if frame is None:
                break
        cam.close()
    """

    def __init__(self, camera_index: int = 0, *, width: int = 1280, height: int = 720, fps: int = 30):
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self.fps = fps
        self._cap = None
        self._info: Optional[CameraInfo] = None

    def open(self) -> bool:
        """Open the camera. Returns True on success."""
        try:
            import cv2
        except ImportError:
            return False

        # Check if CSI
        known = discover_cameras()
        self._info = next((c for c in known if c.index == self.camera_index), None)

        if self._info and self._info.is_csi:
            pipeline = (
                f"nvarguscamerasrc sensor-id={self.camera_index} ! "
                f"video/x-raw(memory:NVMM),width={self.width},height={self.height},"
                f"framerate={self.fps}/1 ! nvvidconv ! video/x-raw,format=BGRx ! "
                f"videoconvert ! video/x-raw,format=BGR ! appsink drop=1"
            )
            self._cap = cv2.VideoCapture(pipeline, cv2.CAP_GSTREAMER)
        else:
            self._cap = cv2.VideoCapture(self.camera_index)
            if self._cap.isOpened():
                self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                self._cap.set(cv2.CAP_PROP_FPS, self.fps)

        return self._cap is not None and self._cap.isOpened()

    def read(self):
        """Read a frame. Returns BGR numpy array or None."""
        if self._cap is None:
            return None
        ret, frame = self._cap.read()
        return frame if ret else None

    def close(self):
        """Release the camera."""
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    @property
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()


# ── JSON/tool output ─────────────────────────────────────────────────────


def list_cameras_json() -> str:
    """Return discovered cameras as JSON (for use as an agent tool)."""
    cameras = discover_cameras()
    return json.dumps({
        "cameras": [asdict(c) for c in cameras],
        "count": len(cameras),
        "platform": platform.system(),
    }, indent=2)


# ── CLI ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cameras = discover_cameras(force_refresh=True)
    if not cameras:
        print("No cameras found.")
        sys.exit(1)
    print(f"\n{'='*60}")
    print(f"  Found {len(cameras)} camera(s)")
    print(f"{'='*60}")
    for c in cameras:
        csi_tag = " [CSI]" if c.is_csi else ""
        print(f"  [{c.index}] {c.name}{csi_tag}")
        print(f"      {c.width}x{c.height} @ {c.fps:.0f}fps  ({c.backend})")
        if c.device_path:
            print(f"      Device: {c.device_path}")
    print()
