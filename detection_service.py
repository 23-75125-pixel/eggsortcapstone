"""YOLO inference helpers for uploaded and live OpenCV frames."""

from __future__ import annotations

import os
from pathlib import Path
from threading import Lock
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
MODEL_PATH = Path(
    os.environ.get("YOLO_MODEL_PATH", str(BASE_DIR / "best.pt"))
).expanduser()
CONFIDENCE = float(os.environ.get("YOLO_CONFIDENCE", "0.25"))
IMAGE_SIZE = int(os.environ.get("YOLO_IMAGE_SIZE", "512"))
MAX_FRAME_BYTES = 5 * 1024 * 1024

_model: Any | None = None
_model_lock = Lock()
_inference_lock = Lock()


class DetectorUnavailableError(RuntimeError):
    """Raised when dependencies or model weights are unavailable."""


class InvalidFrameError(ValueError):
    """Raised when an uploaded frame cannot be decoded."""


def _load_model() -> Any:
    global _model

    if _model is not None:
        return _model

    with _model_lock:
        if _model is not None:
            return _model

        if not MODEL_PATH.is_file():
            raise DetectorUnavailableError(
                f"YOLO model not found at {MODEL_PATH}. Add best.pt to the "
                "project root or set YOLO_MODEL_PATH."
            )

        try:
            from ultralytics import YOLO
        except ImportError as exc:
            raise DetectorUnavailableError(
                "Ultralytics is not installed. Run: pip install -r requirements.txt"
            ) from exc

        try:
            _model = YOLO(str(MODEL_PATH))
        except Exception as exc:
            raise DetectorUnavailableError(
                f"Unable to load YOLO model: {exc}"
            ) from exc

    return _model


def detect_image(frame: Any) -> dict[str, Any]:
    """Run YOLO on an OpenCV image and return JSON-safe detections."""
    model = _load_model()
    try:
        with _inference_lock:
            result = model.predict(
                source=frame,
                conf=CONFIDENCE,
                imgsz=IMAGE_SIZE,
                verbose=False,
            )[0]
    except Exception as exc:
        raise DetectorUnavailableError(f"YOLO inference failed: {exc}") from exc

    names = result.names
    detections: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    if result.boxes is not None:
        boxes = result.boxes.xyxy.cpu().tolist()
        confidences = result.boxes.conf.cpu().tolist()
        class_ids = result.boxes.cls.cpu().tolist()

        for coordinates, confidence, class_id_value in zip(
            boxes, confidences, class_ids
        ):
            class_id = int(class_id_value)
            if isinstance(names, dict):
                label = str(names.get(class_id, class_id))
            else:
                label = str(names[class_id])

            counts[label] = counts.get(label, 0) + 1
            detections.append(
                {
                    "box": [round(float(value), 2) for value in coordinates],
                    "class_id": class_id,
                    "label": label,
                    "confidence": round(float(confidence), 4),
                }
            )

    height, width = frame.shape[:2]
    return {
        "detections": detections,
        "counts": counts,
        "total": len(detections),
        "image_width": width,
        "image_height": height,
        "confidence_threshold": CONFIDENCE,
    }


def detect_frame(frame_bytes: bytes) -> dict[str, Any]:
    """Decode one JPEG/PNG frame and return JSON-safe YOLO detections."""
    if not frame_bytes:
        raise InvalidFrameError("The uploaded camera frame is empty.")
    if len(frame_bytes) > MAX_FRAME_BYTES:
        raise InvalidFrameError("The camera frame exceeds the 5 MB limit.")

    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise DetectorUnavailableError(
            "OpenCV and NumPy are not installed. Run: pip install -r requirements.txt"
        ) from exc

    encoded_frame = np.frombuffer(frame_bytes, dtype=np.uint8)
    frame = cv2.imdecode(encoded_frame, cv2.IMREAD_COLOR)
    if frame is None:
        raise InvalidFrameError("The uploaded file is not a valid image frame.")

    return detect_image(frame)


def annotate_image(frame: Any, result: dict[str, Any]) -> Any:
    """Draw YOLO boxes and labels onto a copy of an OpenCV image."""
    try:
        import cv2
    except ImportError as exc:
        raise DetectorUnavailableError(
            "OpenCV is not installed. Run: pip install -r requirements.txt"
        ) from exc

    annotated = frame.copy()
    for detection in result["detections"]:
        x1, y1, x2, y2 = (int(value) for value in detection["box"])
        class_id = detection["class_id"]
        color = (
            int((class_id * 83 + 40) % 255),
            int((class_id * 47 + 150) % 255),
            int((class_id * 113 + 220) % 255),
        )
        label = (
            f"{detection['label']} "
            f"{round(detection['confidence'] * 100)}%"
        )

        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
        (text_width, text_height), baseline = cv2.getTextSize(
            label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2
        )
        label_top = max(0, y1 - text_height - baseline - 8)
        cv2.rectangle(
            annotated,
            (x1, label_top),
            (x1 + text_width + 10, y1),
            color,
            -1,
        )
        cv2.putText(
            annotated,
            label,
            (x1 + 5, max(text_height + 2, y1 - baseline - 4)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (20, 24, 32),
            2,
            cv2.LINE_AA,
        )

    return annotated
