"""Persistent, low-latency OpenCV camera and YOLO detection session."""

from __future__ import annotations

import os
from collections import deque
from datetime import datetime, timezone
from threading import Condition, Event, RLock, Thread
from time import monotonic
from typing import Any

from detection_service import (
    CONFIDENCE,
    DetectorUnavailableError,
    annotate_image,
    detect_image,
    detector_info,
)


# Exact class set used by the trained five-class egg-quality model.  "no egg"
# is a negative observation: it can break a run of quality samples, but it is
# never saved as an egg quality.
EGG_QUALITY_LABELS = {"crack", "good", "rotten", "undefined"}
NO_EGG_LABEL = "no egg"


def normalize_model_label(value: Any) -> str:
    """Normalize model spelling/case without changing its class meaning."""
    return " ".join(
        str(value).strip().casefold().replace("_", " ").replace("-", " ").split()
    )


class CameraSessionError(RuntimeError):
    """Raised when the server camera cannot be started."""


class CameraDetectionSession:
    """Capture smoothly while YOLO independently processes the newest frame."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._frame_ready = Condition(self._lock)
        self._raw_frame_ready = Condition(self._lock)
        self._quality_ready = Condition(self._lock)
        self._stop_event = Event()
        self._capture_thread: Thread | None = None
        self._inference_thread: Thread | None = None
        self._capture: Any | None = None
        self._running = False

        self._latest_raw_frame: Any | None = None
        self._raw_sequence = 0
        self._inspection_min_sequence = 0
        self._latest_jpeg: bytes | None = None
        self._frame_sequence = 0
        self._latest_result: dict[str, Any] | None = None
        self._quality_history: deque[tuple[float, str, float]] = deque(
            maxlen=200
        )

        self._error: str | None = None
        self._started_at: str | None = None
        self._session_ref: str | None = None
        self._stream_fps = 0.0
        self._detection_fps = 0.0
        self._inference_ms = 0.0
        self._model_info: dict[str, Any] | None = None
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

        # Fail camera startup immediately when YOLO_MODEL_PATH points to the
        # wrong weights or the weights do not contain the trained five-class
        # label set. This avoids showing a running session that can never
        # produce a valid egg record.
        try:
            model_info = detector_info()
        except DetectorUnavailableError as exc:
            raise CameraSessionError(str(exc)) from exc

        with self._lock:
            # Multiple dashboard/status requests can arrive immediately after
            # login. Re-check while holding the lock so only one request may
            # create capture/inference threads and open the physical camera.
            if self._running:
                return self.status()
            self._capture = None
            self._stop_event.clear()
            self._running = True
            self._latest_raw_frame = None
            self._raw_sequence = 0
            self._inspection_min_sequence = 0
            self._latest_jpeg = None
            self._frame_sequence = 0
            self._latest_result = None
            self._quality_history.clear()
            self._error = None
            self._stream_fps = 0.0
            self._detection_fps = 0.0
            self._inference_ms = 0.0
            self._model_info = model_info
            self._started_at = datetime.now(timezone.utc).isoformat()
            self._session_ref = datetime.now().strftime("SES-%Y%m%d-%H%M%S")
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
            self._quality_ready.notify_all()

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
                "session_ref": self._session_ref,
                "frame_ready": self._latest_jpeg is not None,
                "total": result.get("total", 0),
                "counts": result.get("counts", {}),
                "confidence_threshold": result.get(
                    "confidence_threshold", CONFIDENCE
                ),
                "stream_fps": round(self._stream_fps, 1),
                "detection_fps": round(self._detection_fps, 1),
                "inference_ms": round(self._inference_ms),
                "model": dict(self._model_info) if self._model_info else None,
            }

    def quality_snapshot(self, window_seconds: float = 3.0) -> dict[str, Any]:
        """Return the strongest recent camera quality classification."""
        cutoff = monotonic() - window_seconds
        with self._lock:
            recent = [
                (label, confidence)
                for captured_at, label, confidence in self._quality_history
                if captured_at >= cutoff
            ]
            return self._summarize_quality(recent)

    def begin_egg_inspection(self) -> None:
        """Discard detections from earlier eggs before inspecting this egg."""
        with self._quality_ready:
            self._quality_history.clear()
            # Exclude an inference already in progress on a frame captured
            # before the load cell announced this egg.
            self._inspection_min_sequence = self._raw_sequence + 1
            self._quality_ready.notify_all()

    def wait_for_egg_quality(
        self,
        timeout: float = 1.0,
        min_samples: int = 3,
    ) -> dict[str, Any] | None:
        """Wait until consecutive frames agree on one quality for this egg."""
        deadline = monotonic() + timeout
        with self._quality_ready:
            while True:
                # Inference stores exactly one observation per frame. Looking
                # only at the tail means a later "no egg", missing detection,
                # or conflicting class invalidates an older partial match.
                recent = list(self._quality_history)[-min_samples:]
                if len(recent) == min_samples:
                    labels = [sample[1] for sample in recent]
                    label = labels[-1]
                    if label in EGG_QUALITY_LABELS and all(
                        candidate == label for candidate in labels
                    ):
                        confidences = [sample[2] for sample in recent]
                        return {
                            "label": label,
                            "confidence": round(max(confidences), 4),
                        }
                if not self._running or self._error:
                    return None
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return None
                self._quality_ready.wait(timeout=remaining)

    @staticmethod
    def _summarize_quality(
        samples: list[tuple[str, float]],
    ) -> dict[str, Any]:
        if not samples:
            return {"label": "unknown", "confidence": 0.0}
        scores: dict[str, float] = {}
        peaks: dict[str, float] = {}
        for label, confidence in samples:
            scores[label] = scores.get(label, 0.0) + confidence
            peaks[label] = max(peaks.get(label, 0.0), confidence)
        label = max(scores, key=scores.get)
        return {"label": label, "confidence": round(peaks[label], 4)}

    @staticmethod
    def _select_frame_observation(
        detections: list[dict[str, Any]],
    ) -> tuple[str, float]:
        """Choose one unambiguous model observation for a camera frame.

        A frame can contain overlapping YOLO boxes. Selecting one recognized
        class with the highest confidence prevents one physical egg from
        contributing several contradictory samples to the same cycle.
        """
        recognized: list[tuple[str, float]] = []
        for detection in detections:
            label = normalize_model_label(detection.get("label", ""))
            if label in EGG_QUALITY_LABELS or label == NO_EGG_LABEL:
                recognized.append(
                    (label, float(detection.get("confidence", 0.0)))
                )
        if not recognized:
            return NO_EGG_LABEL, 0.0
        return max(recognized, key=lambda item: item[1])

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
                self._quality_ready.notify_all()
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
                with self._quality_ready:
                    self._latest_result = result
                    captured_at = monotonic()
                    if processed_sequence >= self._inspection_min_sequence:
                        label, confidence = self._select_frame_observation(
                            result["detections"]
                        )
                        self._quality_history.append(
                            (captured_at, label, confidence)
                        )
                    self._quality_ready.notify_all()
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
            self._quality_ready.notify_all()


CAMERA_SESSION = CameraDetectionSession()
