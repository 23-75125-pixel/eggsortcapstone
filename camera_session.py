"""Persistent, low-latency OpenCV camera and YOLO detection session."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from threading import Condition, Event, RLock, Thread
from time import monotonic
from typing import Any

from detection_service import (
    CONFIDENCE,
    annotate_image,
    detect_image,
)


class CameraSessionError(RuntimeError):
    """Raised when the server camera cannot be started."""


class CameraDetectionSession:
    """Capture smoothly while YOLO independently processes the newest frame."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._frame_ready = Condition(self._lock)
        self._raw_frame_ready = Condition(self._lock)
        self._stop_event = Event()
        self._capture_thread: Thread | None = None
        self._inference_thread: Thread | None = None
        self._capture: Any | None = None
        self._running = False

        self._latest_raw_frame: Any | None = None
        self._raw_sequence = 0
        self._latest_jpeg: bytes | None = None
        self._frame_sequence = 0
        self._latest_result: dict[str, Any] | None = None

        self._error: str | None = None
        self._started_at: str | None = None
        self._stream_fps = 0.0
        self._detection_fps = 0.0
        self._inference_ms = 0.0
        self.camera_index = int(os.environ.get("CAMERA_INDEX", "0"))
        self.camera_backend = os.environ.get(
            "CAMERA_BACKEND",
            "dshow" if os.name == "nt" else "auto",
        ).lower()
        self.camera_width = int(os.environ.get("CAMERA_WIDTH", "1280"))
        self.camera_height = int(os.environ.get("CAMERA_HEIGHT", "720"))
        self.camera_fps = int(os.environ.get("CAMERA_FPS", "30"))
        self.jpeg_quality = int(os.environ.get("CAMERA_JPEG_QUALITY", "72"))

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._running:
                return self.status()

        try:
            import cv2
        except ImportError as exc:
            raise CameraSessionError(
                "OpenCV is not installed. Run: pip install -r requirements.txt"
            ) from exc

        with self._lock:
            self._capture = None
            self._stop_event.clear()
            self._running = True
            self._latest_raw_frame = None
            self._raw_sequence = 0
            self._latest_jpeg = None
            self._frame_sequence = 0
            self._latest_result = None
            self._error = None
            self._stream_fps = 0.0
            self._detection_fps = 0.0
            self._inference_ms = 0.0
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._capture_thread = Thread(
                target=self._capture_loop,
                name="eggsort-camera-capture",
                daemon=True,
            )
            self._inference_thread = Thread(
                target=self._inference_loop,
                name="eggsort-yolo-inference",
                daemon=True,
            )
            self._capture_thread.start()
            self._inference_thread.start()
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            capture_thread = self._capture_thread
            inference_thread = self._inference_thread
            self._stop_event.set()
            self._frame_ready.notify_all()
            self._raw_frame_ready.notify_all()

        if capture_thread and capture_thread.is_alive():
            capture_thread.join(timeout=5)
        if inference_thread and inference_thread.is_alive():
            inference_thread.join(timeout=15)

        with self._lock:
            threads_alive = any(
                thread and thread.is_alive()
                for thread in (capture_thread, inference_thread)
            )
            if not threads_alive:
                self._running = False
                self._capture_thread = None
                self._inference_thread = None
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            result = self._latest_result or {}
            return {
                "running": self._running,
                "error": self._error,
                "started_at": self._started_at,
                "frame_ready": self._latest_jpeg is not None,
                "total": result.get("total", 0),
                "counts": result.get("counts", {}),
                "confidence_threshold": result.get(
                    "confidence_threshold", CONFIDENCE
                ),
                "stream_fps": round(self._stream_fps, 1),
                "detection_fps": round(self._detection_fps, 1),
                "inference_ms": round(self._inference_ms),
            }

    def wait_for_frame(
        self, previous_sequence: int, timeout: float = 2.0
    ) -> tuple[int, bytes | None, bool]:
        with self._frame_ready:
            self._frame_ready.wait_for(
                lambda: (
                    self._frame_sequence != previous_sequence
                    or not self._running
                ),
                timeout=timeout,
            )
            return (
                self._frame_sequence,
                self._latest_jpeg,
                self._running,
            )

    def _capture_loop(self) -> None:
        import cv2

        fps_started = monotonic()
        fps_frames = 0
        try:
            backend_codes = {
                "auto": cv2.CAP_ANY,
                "dshow": cv2.CAP_DSHOW,
                "msmf": cv2.CAP_MSMF,
            }
            if self.camera_backend not in backend_codes:
                raise CameraSessionError(
                    "CAMERA_BACKEND must be auto, dshow, or msmf."
                )

            capture = cv2.VideoCapture(
                self.camera_index,
                backend_codes[self.camera_backend],
            )
            if not capture.isOpened():
                capture.release()
                raise CameraSessionError(
                    f"Unable to open camera index {self.camera_index} with "
                    f"the {self.camera_backend} backend. Close other camera "
                    "apps or change CAMERA_INDEX/CAMERA_BACKEND."
                )

            # MJPG avoids the USB 2.0 bandwidth ceiling that commonly limits
            # uncompressed 720p webcams to roughly 5-10 FPS.
            capture.set(
                cv2.CAP_PROP_FOURCC,
                cv2.VideoWriter_fourcc(*"MJPG"),
            )
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.camera_width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.camera_height)
            capture.set(cv2.CAP_PROP_FPS, self.camera_fps)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            with self._lock:
                self._capture = capture

            while not self._stop_event.is_set():
                with self._lock:
                    capture = self._capture
                if capture is None:
                    break

                success, frame = capture.read()
                if not success:
                    raise CameraSessionError(
                        "The camera stopped returning frames."
                    )

                # Give inference only the newest frame. No queue means no
                # increasing detection delay when the model is slower than video.
                with self._raw_frame_ready:
                    self._latest_raw_frame = frame
                    self._raw_sequence += 1
                    result = self._latest_result
                    self._raw_frame_ready.notify()

                display_frame = (
                    annotate_image(frame, result)
                    if result is not None
                    else frame
                )
                encoded, jpeg = cv2.imencode(
                    ".jpg",
                    display_frame,
                    [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality],
                )
                if not encoded:
                    raise CameraSessionError(
                        "OpenCV could not encode the camera frame."
                    )

                fps_frames += 1
                elapsed = monotonic() - fps_started
                with self._frame_ready:
                    if elapsed >= 1.0:
                        self._stream_fps = fps_frames / elapsed
                        fps_started = monotonic()
                        fps_frames = 0
                    self._latest_jpeg = jpeg.tobytes()
                    self._frame_sequence += 1
                    self._frame_ready.notify_all()
        except Exception as exc:
            self._fail(str(exc))
        finally:
            with self._frame_ready:
                if self._capture is not None:
                    self._capture.release()
                self._capture = None
                self._running = False
                self._capture_thread = None
                self._stop_event.set()
                self._raw_frame_ready.notify_all()
                self._frame_ready.notify_all()

    def _inference_loop(self) -> None:
        processed_sequence = 0
        try:
            while not self._stop_event.is_set():
                with self._raw_frame_ready:
                    self._raw_frame_ready.wait_for(
                        lambda: (
                            self._raw_sequence != processed_sequence
                            or self._stop_event.is_set()
                        ),
                        timeout=1.0,
                    )
                    if self._stop_event.is_set():
                        break
                    if (
                        self._raw_sequence == processed_sequence
                        or self._latest_raw_frame is None
                    ):
                        continue
                    frame = self._latest_raw_frame.copy()
                    processed_sequence = self._raw_sequence

                started = monotonic()
                result = detect_image(frame)
                elapsed = monotonic() - started
                with self._lock:
                    self._latest_result = result
                    self._inference_ms = elapsed * 1000
                    self._detection_fps = 1 / elapsed if elapsed else 0.0
        except Exception as exc:
            self._fail(str(exc))
        finally:
            with self._lock:
                self._inference_thread = None

    def _fail(self, message: str) -> None:
        with self._frame_ready:
            self._error = message
            self._stop_event.set()
            self._frame_ready.notify_all()
            self._raw_frame_ready.notify_all()


CAMERA_SESSION = CameraDetectionSession()
