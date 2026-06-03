"""
repryntt.hardware.camera_broker — Single producer per CSI sensor.

Why this exists: the IMX219 cameras on Jetson are single-consumer through
nvargus-daemon. Multiple call sites in the codebase used to open them
independently (cv2.VideoCapture, gst-launch shells, Flask streamers),
causing RAM contention on the 7.4 GB Orin Nano and producing corrupt
"static" frames whenever a fallback path read raw Bayer as YUYV.

The broker holds the only handle. Consumers ask for the latest frame
via `broker.get_latest(sensor_id)` and never open /dev/video* themselves.

Design:
    - One producer thread per sensor; one nvarguscamerasrc pipeline.
    - Latest BGR frame kept in memory under a lock + condition variable.
    - Lazy spin-up on first get_latest; idle shutdown after N seconds.
    - flock(LOCK_EX) on /dev/video{N} so cross-process consumers fail loud.
    - Optional MJPEG endpoint for cross-process previews (off by default).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import cv2
    _ORIGINAL_VIDEO_CAPTURE = cv2.VideoCapture
    CV2_AVAILABLE = True
except ImportError:
    cv2 = None  # type: ignore
    _ORIGINAL_VIDEO_CAPTURE = None
    CV2_AVAILABLE = False

try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    np = None  # type: ignore
    NUMPY_AVAILABLE = False


# ── Defaults (overridden by ai_config.json) ─────────────────────────────

_DEFAULT_CONFIG = {
    "enabled": True,
    "sensors": [
        {"id": 0, "width": 1280, "height": 720, "fps": 30},
        {"id": 1, "width": 1280, "height": 720, "fps": 30},
    ],
    "idle_timeout_s": 30.0,
    "stereo_pair": [0, 1],
    "mjpeg_port": None,
}


def _load_config() -> Dict:
    """Load camera_broker config from the runtime ai_config.json.

    Resolves to the same path repryntt's daemon reads
    (Path.home() / ".repryntt" / "brain" / "ai_config.json").
    """
    cfg_path = Path.home() / ".repryntt" / "brain" / "ai_config.json"
    base = dict(_DEFAULT_CONFIG)
    try:
        with open(cfg_path, "r") as f:
            raw = json.load(f)
        section = raw.get("camera_broker") or {}
        for k, v in section.items():
            base[k] = v
    except Exception:
        pass
    return base


def _gst_argv(sensor_id: int, width: int, height: int, fps: int) -> List[str]:
    """Build a gst-launch-1.0 argv that pipes raw BGR frames to stdout.

    We drive GStreamer via subprocess (not cv2) because the prebuilt
    OpenCV wheels on Jetson don't include GStreamer support — every
    cv2.VideoCapture(..., CAP_GSTREAMER) call returns isOpened()==False.
    fdsink fd=1 streams raw BGR frames straight to our stdout pipe.
    """
    return [
        "gst-launch-1.0", "-q",
        "nvarguscamerasrc", f"sensor-id={sensor_id}",
        "!", f"video/x-raw(memory:NVMM),width={width},height={height},"
             f"format=NV12,framerate={fps}/1",
        "!", "nvvidconv",
        "!", "video/x-raw,format=BGRx",
        "!", "videoconvert",
        "!", "video/x-raw,format=BGR",
        "!", "fdsink", "fd=1", "sync=false",
    ]


# ── Per-sensor producer ─────────────────────────────────────────────────


@dataclass
class _SensorProducer:
    """Owns a single nvarguscamerasrc pipeline + the latest frame slot."""

    sensor_id: int
    width: int
    height: int
    fps: int
    idle_timeout_s: float

    _thread: Optional[threading.Thread] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _cond: threading.Condition = field(init=False)
    _stop: threading.Event = field(default_factory=threading.Event)
    _latest_frame: object = None  # numpy.ndarray, BGR
    _latest_ts_ms: float = 0.0
    _frame_seq: int = 0
    _last_consumer_at: float = 0.0
    _refcount: int = 0
    _device_fd: Optional[int] = None
    _proc: Optional[subprocess.Popen] = None
    _started_at: float = 0.0

    def __post_init__(self):
        self._cond = threading.Condition(self._lock)

    def _claim_device_lock(self) -> bool:
        """flock(LOCK_EX) on /dev/video{id} to refuse parallel opens."""
        path = f"/dev/video{self.sensor_id}"
        try:
            fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NONBLOCK)
        except FileNotFoundError:
            logger.warning("camera_broker: %s not found", path)
            return False
        except PermissionError as e:
            logger.warning("camera_broker: %s permission denied: %s", path, e)
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            logger.error(
                "camera_broker: %s already locked by another process — "
                "refusing to start producer (this is the duplicate-opener guard)",
                path,
            )
            return False
        self._device_fd = fd
        return True

    def _release_device_lock(self) -> None:
        if self._device_fd is not None:
            try:
                fcntl.flock(self._device_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(self._device_fd)
            except OSError:
                pass
            self._device_fd = None

    def _open_pipeline(self) -> bool:
        if shutil.which("gst-launch-1.0") is None:
            logger.error("camera_broker: gst-launch-1.0 not on PATH")
            return False
        argv = _gst_argv(self.sensor_id, self.width, self.height, self.fps)
        try:
            self._proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                preexec_fn=os.setsid,
            )
        except Exception as e:
            logger.error("camera_broker: failed to spawn gst-launch: %s", e)
            return False
        return True

    def _producer_loop(self) -> None:
        """Read raw BGR frames from gst-launch stdout, write into slot."""
        if not NUMPY_AVAILABLE:
            logger.error("camera_broker: numpy unavailable, cannot decode frames")
            return
        if not self._open_pipeline() or self._proc is None or self._proc.stdout is None:
            return
        self._started_at = time.time()
        frame_bytes = self.width * self.height * 3
        logger.info(
            "camera_broker: sensor %d producer started (%dx%d@%dfps, %d B/frame)",
            self.sensor_id, self.width, self.height, self.fps, frame_bytes,
        )
        stdout = self._proc.stdout
        # IMX219 AE/AWB takes ~15-30 frames to converge — drop the first
        # batch so consumers never see a dim, pre-converged frame.
        warmup_frames = max(0, int(self.fps))  # ~1 s of warmup
        frames_seen = 0
        try:
            while not self._stop.is_set():
                # Read exactly one frame's worth of raw BGR bytes.
                buf = bytearray(frame_bytes)
                view = memoryview(buf)
                got = 0
                while got < frame_bytes:
                    chunk = stdout.read(frame_bytes - got)
                    if not chunk:
                        # gst-launch died or closed stdout
                        if self._stop.is_set():
                            return
                        logger.warning(
                            "camera_broker: sensor %d gst-launch closed stdout — exiting producer",
                            self.sensor_id,
                        )
                        return
                    view[got:got + len(chunk)] = chunk
                    got += len(chunk)
                frames_seen += 1
                if frames_seen <= warmup_frames:
                    continue  # AE/AWB still settling
                frame = np.frombuffer(buf, dtype=np.uint8).reshape(
                    self.height, self.width, 3,
                )
                ts_ms = time.time() * 1000.0
                with self._cond:
                    self._latest_frame = frame
                    self._latest_ts_ms = ts_ms
                    self._frame_seq += 1
                    self._cond.notify_all()
                # Cross-process snapshot for processes that lost the flock
                # race (e.g. nexus_app teleop while daemon owns the camera).
                # Throttle to ~5 fps so we don't burn CPU on jpeg encode.
                if self._frame_seq % max(1, int(self.fps / 5)) == 0:
                    self._write_shm_snapshot(frame)
                    if (
                        self._refcount == 0
                        and self._last_consumer_at > 0
                        and (time.time() - self._last_consumer_at) > self.idle_timeout_s
                    ):
                        logger.info(
                            "camera_broker: sensor %d idle for >%.0fs — shutting down",
                            self.sensor_id, self.idle_timeout_s,
                        )
                        return
        finally:
            self._terminate_proc()
            self._release_device_lock()
            logger.info("camera_broker: sensor %d producer stopped", self.sensor_id)

    def _write_shm_snapshot(self, frame) -> None:
        """Encode frame as JPEG and atomically replace the shm snapshot file."""
        try:
            import cv2  # local import — we already require it for nav
        except ImportError:
            return
        path = shm_snapshot_path(self.sensor_id)
        tmp = path + ".tmp"
        try:
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
            if not ok:
                return
            with open(tmp, "wb") as f:
                f.write(buf.tobytes())
            os.replace(tmp, path)
        except Exception as e:
            logger.debug("camera_broker: shm snapshot write failed: %s", e)

    def _terminate_proc(self) -> None:
        if self._proc is None:
            return
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            self._proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                self._proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
        self._proc = None

    def start(self) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return True
        if not self._claim_device_lock():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._producer_loop,
            name=f"camera-broker-cam{self.sensor_id}",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def get_latest(self, max_age_ms: Optional[float] = None,
                   timeout_s: float = 2.0):
        """Return (frame, ts_ms) or (None, None).

        With max_age_ms set, blocks up to timeout_s waiting for a fresh frame.
        """
        deadline = time.time() + timeout_s
        with self._cond:
            self._refcount += 1
            self._last_consumer_at = time.time()
        try:
            while True:
                with self._cond:
                    if self._latest_frame is not None:
                        age_ms = (time.time() * 1000.0) - self._latest_ts_ms
                        if max_age_ms is None or age_ms <= max_age_ms:
                            # Return a copy so callers can't mutate the slot.
                            frame = (
                                self._latest_frame.copy()
                                if NUMPY_AVAILABLE
                                else self._latest_frame
                            )
                            return frame, self._latest_ts_ms
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        return None, None
                    if max_age_ms is None:
                        # No freshness requirement and no frame yet — wait briefly.
                        self._cond.wait(timeout=min(remaining, 0.5))
                    else:
                        self._cond.wait(timeout=min(remaining, max_age_ms / 1000.0))
        finally:
            with self._cond:
                self._refcount = max(0, self._refcount - 1)
                self._last_consumer_at = time.time()

    def metadata(self) -> Dict:
        with self._lock:
            return {
                "sensor_id": self.sensor_id,
                "width": self.width,
                "height": self.height,
                "fps": self.fps,
                "running": self._thread is not None and self._thread.is_alive(),
                "last_capture_ms": self._latest_ts_ms,
                "frame_seq": self._frame_seq,
            }


# ── Broker singleton ────────────────────────────────────────────────────


class CameraBroker:
    """Owns a producer per CSI sensor and serves the latest frames."""

    def __init__(self):
        self._cfg = _load_config()
        self._producers: Dict[int, _SensorProducer] = {}
        self._global_lock = threading.Lock()
        self._guard_installed = False

    # -- producer lookup ----------------------------------------------

    def _producer(self, sensor_id: int) -> Optional[_SensorProducer]:
        with self._global_lock:
            existing = self._producers.get(sensor_id)
            if existing is not None and existing._thread is not None and existing._thread.is_alive():
                return existing

            cfg_for = next(
                (s for s in self._cfg.get("sensors", []) if s.get("id") == sensor_id),
                None,
            )
            if cfg_for is None:
                cfg_for = {
                    "id": sensor_id,
                    "width": 1280,
                    "height": 720,
                    "fps": 30,
                }

            producer = _SensorProducer(
                sensor_id=sensor_id,
                width=int(cfg_for.get("width", 1280)),
                height=int(cfg_for.get("height", 720)),
                fps=int(cfg_for.get("fps", 30)),
                idle_timeout_s=float(self._cfg.get("idle_timeout_s", 30.0)),
            )
            if not producer.start():
                return None
            # Install the direct-cv2 guard once the broker is actually live —
            # this is what the user asked for: "make sure this is the only
            # autonomous cam use".
            if not self._guard_installed:
                try:
                    from repryntt.hardware._camera_guard import install_guard
                    install_guard()
                    self._guard_installed = True
                except Exception as e:
                    logger.debug("camera_broker: guard install failed: %s", e)
            self._producers[sensor_id] = producer
            return producer

    # -- public API ----------------------------------------------------

    def get_latest(
        self,
        sensor_id: int,
        max_age_ms: Optional[float] = None,
        timeout_s: float = 2.0,
    ):
        """Return (frame, ts_ms) — BGR numpy array + capture timestamp.

        If max_age_ms is set, blocks up to timeout_s waiting for a frame
        no older than that. Otherwise returns the latest available.
        """
        if not self._cfg.get("enabled", True):
            return None, None
        prod = self._producer(sensor_id)
        if prod is None:
            return None, None
        return prod.get_latest(max_age_ms=max_age_ms, timeout_s=timeout_s)

    def get_latest_pair(
        self,
        sensor_ids: Tuple[int, int] = (0, 1),
        sync_tolerance_ms: float = 50.0,
        max_age_ms: Optional[float] = None,
        timeout_s: float = 2.0,
    ):
        """Return (left_frame, right_frame, mid_ts_ms) or (None, None, None).

        Caller specifies (left_id, right_id). The pair is considered
        synchronized when their timestamps differ by < sync_tolerance_ms.
        """
        left_id, right_id = sensor_ids
        deadline = time.time() + timeout_s
        last_left = last_right = None
        last_left_ts = last_right_ts = 0.0
        while time.time() < deadline:
            l_frame, l_ts = self.get_latest(left_id, max_age_ms=max_age_ms, timeout_s=0.5)
            r_frame, r_ts = self.get_latest(right_id, max_age_ms=max_age_ms, timeout_s=0.5)
            if l_frame is not None and r_frame is not None:
                if abs(l_ts - r_ts) <= sync_tolerance_ms:
                    return l_frame, r_frame, (l_ts + r_ts) / 2.0
                last_left, last_left_ts = l_frame, l_ts
                last_right, last_right_ts = r_frame, r_ts
            time.sleep(0.02)
        # Best-effort: return whatever we last got, even if not perfectly synced.
        if last_left is not None and last_right is not None:
            return last_left, last_right, (last_left_ts + last_right_ts) / 2.0
        return None, None, None

    def metadata(self, sensor_id: Optional[int] = None) -> Dict:
        with self._global_lock:
            if sensor_id is None:
                return {
                    "config": self._cfg,
                    "producers": {
                        sid: p.metadata() for sid, p in self._producers.items()
                    },
                }
            prod = self._producers.get(sensor_id)
            return prod.metadata() if prod else {"sensor_id": sensor_id, "running": False}

    def shutdown(self) -> None:
        with self._global_lock:
            producers = list(self._producers.values())
            self._producers.clear()
        for p in producers:
            p.stop()


# Module-level singleton.
broker = CameraBroker()


__all__ = ["broker", "CameraBroker", "shm_snapshot_path"]


def shm_snapshot_path(sensor_id: int) -> str:
    """Path to the cross-process latest-frame snapshot for this sensor.

    Whichever process won the flock writes JPEGs here at ~5 fps so other
    processes (e.g. teleop in nexus_app while the daemon owns the camera)
    can show a near-live preview without contending for /dev/video*.
    """
    base = "/dev/shm" if os.path.isdir("/dev/shm") else "/tmp"
    return os.path.join(base, f"repryntt_cam{sensor_id}_latest.jpg")
