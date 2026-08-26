"""Face presence / count inference module.

Wraps the MediaPipe Tasks API ``FaceDetector`` (BlazeFace short-range
detector — the same underlying model the removed ``mp.solutions``
used).  The model bundle (``.task`` file) is **not** bundled in the
``mediapipe`` pip wheel; it must be baked into the container image at
build time and its path passed via the ``MP_FACE_DETECTOR_BUNDLE``
environment variable.

**Correction (2026-07-24):** ``mp.solutions.face_detection`` is no
longer available in ``mediapipe`` 0.10.31+.  The Tasks API
``FaceDetector`` is the direct replacement with the same detector
model and an identical detection output shape (bounding box +
confidence per detected face).

This module is a **stateless per-frame classifier**.  The 2–3
consecutive-frame confirmation window described in
``docs/proctoring-engine-v1-spec.md`` §3 ("Zero-tolerance rule") is
enforced by the *fusion engine* (the next layer), not here.  This
module's responsibility ends at returning the count + bounding boxes
for the single frame it was given.

Input:  a :class:`~proctoring_engine.preprocessing.frames.DecodedFrame`
        in **RGB** channel order (the output of
        :func:`~proctoring_engine.preprocessing.frames.normalize_for_mediapipe`).
Output: a :class:`FacePresenceResult`.
"""

from __future__ import annotations

import logging
import os
from typing import Final

import numpy as np

from proctoring_engine.inference._types import (
    BoundingBox,
    ConfidenceInterval,
    FacePresenceResult,
)
from proctoring_engine.preprocessing.frames import DecodedFrame


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MP_FACE_DETECTOR_BUNDLE_ENV: Final[str] = "MP_FACE_DETECTOR_BUNDLE"
"""Environment variable that must point to the ``.task`` model bundle."""

# Event type strings emitted by this module.
EVENT_ONE_FACE: Final[str] = "one_face"
EVENT_NO_FACE: Final[str] = "no_face"
EVENT_SECOND_PERSON: Final[str] = "second_person"

# Modality identifier matching ``TelemetryModality.FACE``.
_MODALITY: Final[str] = "face"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class FaceDetectorRunner:
    """Stateless per-frame face detector backed by MediaPipe Tasks API.

    **Architectural note on live dispatch:** This class is an offline/manual-verification
    utility for backend testing and standalone Python frame inspection. It is intentionally
    not wired into the live WebSocket dispatch path (``FrameDispatcher``). In production,
    face-presence telemetry is produced client-side via ``TelemetryLight`` events, while
    server-side heavy-frame face detection and landmarking are handled by
    ``FaceLandmarkerRunner``.

    Parameters
    ----------
    model_bundle_path:
        Filesystem path to the ``.task`` model bundle.  If ``None``,
        the constructor reads ``MP_FACE_DETECTOR_BUNDLE`` from the
        environment.
    min_detection_confidence:
        Minimum confidence score for a detection to be included.
        Default 0.5 — the same default BlazeFace uses internally.
        This is a constructor argument, not a hardcoded threshold:
        callers can tune it without subclassing.

    Raises
    ------
    EnvironmentError
        If ``model_bundle_path`` is ``None`` and the environment
        variable is not set.
    FileNotFoundError
        If the resolved path does not exist.
    """

    def __init__(
        self,
        *,
        model_bundle_path: str | None = None,
        min_detection_confidence: float = 0.5,
    ) -> None:
        if model_bundle_path is None:
            model_bundle_path = os.environ.get(MP_FACE_DETECTOR_BUNDLE_ENV)
        if model_bundle_path is None:
            raise EnvironmentError(
                f"The {MP_FACE_DETECTOR_BUNDLE_ENV} environment variable "
                f"is not set and no model_bundle_path was provided. "
                f"Download the .task model bundle from "
                f"storage.googleapis.com and set the env var."
            )
        if not os.path.isfile(model_bundle_path):
            raise FileNotFoundError(
                f"MediaPipe FaceDetector model bundle not found at "
                f"'{model_bundle_path}'. Bake it into the container image "
                f"at build time."
            )

        self._min_confidence = min_detection_confidence

        # Lazy import so the module can be imported without mediapipe
        # being fully initialised (e.g. during unit tests that mock
        # the env var check).
        import mediapipe as mp

        base_options = mp.tasks.BaseOptions(
            model_asset_path=model_bundle_path,
        )
        options = mp.tasks.vision.FaceDetectorOptions(
            base_options=base_options,
            min_detection_confidence=min_detection_confidence,
        )
        self._detector = mp.tasks.vision.FaceDetector.create_from_options(options)

    def run(self, frame: DecodedFrame) -> FacePresenceResult:
        """Detect faces in a single preprocessed frame.

        Parameters
        ----------
        frame:
            A :class:`DecodedFrame` in **RGB** channel order
            (output of ``normalize_for_mediapipe``).

        Returns
        -------
        FacePresenceResult
            Contains the face count, per-face bounding boxes, and a
            confidence interval.  The ``event_type`` is one of
            ``"one_face"``, ``"no_face"``, or ``"second_person"``.
        """

        import mediapipe as mp

        mp_image = mp.Image(
            image_format=mp.ImageFormat.SRGB,
            data=frame.array,
        )
        detection_result = self._detector.detect(mp_image)
        detections = detection_result.detections

        face_count = len(detections)
        bounding_boxes: list[BoundingBox] = []
        confidences: list[float] = []

        for det in detections:
            # MediaPipe Tasks API returns the bounding box as a
            # BoundingBox protobuf with origin_x, origin_y, width, height
            # in **pixel coordinates** (in the same space as the input
            # frame).  This differs from the deprecated
            # ``mp.solutions.face_detection`` API which returned
            # already-normalised [0, 1] coordinates — dividing by
            # frame.width / frame.height here is REQUIRED, not a no-op.
            # DO NOT simplify this code to "normalize by 1.0" thinking
            # the box is already normalised; doing so will silently
            # produce wildly wrong bounding boxes that exceed the
            # image's pixel dimensions by 100x.
            bbox = det.bounding_box
            bounding_boxes.append(
                BoundingBox(
                    x=bbox.origin_x / frame.width if frame.width > 0 else 0.0,
                    y=bbox.origin_y / frame.height if frame.height > 0 else 0.0,
                    w=bbox.width / frame.width if frame.width > 0 else 0.0,
                    h=bbox.height / frame.height if frame.height > 0 else 0.0,
                )
            )
            if det.categories:
                confidences.append(det.categories[0].score)

        if face_count == 0:
            event_type = EVENT_NO_FACE
            confidence = ConfidenceInterval(lower=0.0, score=0.0, upper=0.0)
        elif face_count == 1:
            event_type = EVENT_ONE_FACE
            score = confidences[0] if confidences else self._min_confidence
            confidence = ConfidenceInterval(lower=score, score=score, upper=score)
        else:
            event_type = EVENT_SECOND_PERSON
            # Worst-case (minimum) confidence across all faces: the
            # weakest detection is the one that could be noise.
            score = min(confidences) if confidences else self._min_confidence
            confidence = ConfidenceInterval(lower=score, score=score, upper=score)

        return FacePresenceResult(
            modality=_MODALITY,
            event_type=event_type,
            confidence=confidence,
            bounding_boxes=bounding_boxes,
            raw_value={
                "face_count": face_count,
                "per_face_confidence": confidences,
            },
            face_count=face_count,
        )

    def close(self) -> None:
        """Release the underlying MediaPipe detector resources."""
        self._detector.close()

    def __enter__(self) -> FaceDetectorRunner:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
