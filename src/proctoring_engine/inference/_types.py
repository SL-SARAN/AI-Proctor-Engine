"""Shared result types for the inference modules layer.

Every inference module returns one of the typed ``*Result`` dataclasses
defined here.  These are *pure data* — no ORM, no side effects, no
network calls.  They are the hand-off contract between the stateless
inference layer and the fusion engine that sits above it.

The ``ConfidenceInterval`` triple ``(lower, score, upper)`` maps
directly to the ``Flag`` schema's ``(confidence_lower, confidence_score,
confidence_upper)`` triple (``src/proctoring_engine/models.py``),
satisfying the spec's requirement that every flag carries a
*statistical* confidence interval, not just a point estimate.

For single-frame inference modules (face presence, object detection,
browser events), the interval is typically ``(score, score, score)``
— a degenerate point interval.  The multi-frame interval is computed
by the *fusion engine* (the next layer), which tracks rolling windows
and builds the spread across frames.  This design keeps inference
modules stateless while preserving the typed contract the fusion
engine needs.

``BoundingBox`` uses normalised ``[0, 1]`` image coordinates so the
same box can be persisted unchanged regardless of the frame resolution
the heavy frame happened to arrive at.  The ``x, y, w, h`` convention
matches the ``TelemetryLightPayload.bbox`` field on the ingestion
envelope (``src/proctoring_engine/websocket/client.py``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# ConfidenceInterval
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    """A ``(lower, score, upper)`` triple in ``[0, 1]``.

    ``lower <= score <= upper`` is enforced at construction time.
    A degenerate interval (``lower == score == upper``) is valid and
    represents a single-frame point estimate.
    """

    lower: float
    score: float
    upper: float

    def __post_init__(self) -> None:
        for name, value in [
            ("lower", self.lower),
            ("score", self.score),
            ("upper", self.upper),
        ]:
            if not (0.0 <= value <= 1.0):
                raise ValueError(
                    f"ConfidenceInterval.{name} must be in [0, 1]; got {value}."
                )
        if self.lower > self.score:
            raise ValueError(
                f"ConfidenceInterval.lower ({self.lower}) must be "
                f"<= score ({self.score})."
            )
        if self.score > self.upper:
            raise ValueError(
                f"ConfidenceInterval.score ({self.score}) must be "
                f"<= upper ({self.upper})."
            )


# ---------------------------------------------------------------------------
# BoundingBox
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class BoundingBox:
    """An image bounding box normalised to ``[0, 1]``.

    ``x, y`` is the top-left corner; ``w, h`` are the width and height.
    All four values must be in ``[0, 1]``.  The box may extend to the
    edge of the image (``x + w == 1.0`` is valid).
    """

    x: float
    y: float
    w: float
    h: float

    def __post_init__(self) -> None:
        for name, value in [
            ("x", self.x),
            ("y", self.y),
            ("w", self.w),
            ("h", self.h),
        ]:
            if not (0.0 <= value <= 1.0):
                raise ValueError(
                    f"BoundingBox.{name} must be in [0, 1]; got {value}."
                )

    def to_dict(self) -> dict[str, float]:
        """Serialise to a JSON-compatible dict for the telemetry payload."""
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}


# ---------------------------------------------------------------------------
# Base inference result
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class InferenceResult:
    """Base inference result emitted by every modality module.

    ``modality`` must match one of the
    ``TelemetryModality`` enum values (``face``, ``gaze``, ``identity``,
    ``object``, ``audio``, ``browser``, ``system``).  ``event_type`` is
    a module-defined string (e.g. ``"one_face"``, ``"off_screen"``).

    ``bounding_boxes`` carries zero or more normalised bounding boxes.
    ``raw_value`` carries modality-specific data (landmark coordinates,
    YOLO class names, EAR values, etc.) for auditability.
    """

    modality: str
    event_type: str
    confidence: ConfidenceInterval
    bounding_boxes: list[BoundingBox] = field(default_factory=list)
    raw_value: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Modality-specific result subclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FacePresenceResult(InferenceResult):
    """Result of the face-presence / face-count modality.

    ``face_count`` is the number of faces detected in the frame.
    The ``event_type`` convention:

    - ``"one_face"`` — exactly one face, normal operation.
    - ``"no_face"`` — zero faces detected (student may have left).
    - ``"second_person"`` — two or more faces (zero-tolerance rule).
    """

    face_count: int = 0


@dataclass(frozen=True, slots=True)
class IdentityMatchResult(InferenceResult):
    """Result of the identity-match modality (single-frame point estimate).

    ``similarity`` is the cosine similarity between the current face
    embedding and the enrollment embedding, in ``[0, 1]``.

    The confidence interval for a *flag* is built by the fusion engine
    from multiple ``IdentityMatchResult`` values across a sampling
    window (``docs/04-inference-modules-design.md`` §2).  This module
    emits a point estimate per invocation.
    """

    similarity: float = 0.0


@dataclass(frozen=True, slots=True)
class HeadPoseGazeResult(InferenceResult):
    """Per-frame head-pose / gaze classification.

    ``off_screen`` is the binary classification; the fusion engine
    aggregates consecutive ``off_screen=True`` frames into
    ``GazeAwayEvent`` objects (``docs/proctoring-engine-v1-spec.md``
    §3.1 Stage 2).

    The ``raw_value`` carries the underlying signals:
    ``yaw``, ``pitch``, ``roll``, ``ear``, ``iris_offset``.
    """

    off_screen: bool = False
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0
    ear: float = 0.0
    iris_offset: float = 0.0


@dataclass(frozen=True, slots=True)
class ObjectDetectionResult(InferenceResult):
    """A single denylist object detected in the frame.

    One ``ObjectDetectionResult`` is emitted per detected denylist
    object.  Non-denylist detections are discarded before reaching
    this result.
    """

    detected_class: str = ""


@dataclass(frozen=True, slots=True)
class AudioVadResult(InferenceResult):
    """Result of audio voice-activity detection across a chunk's frames.

    ``speech_ratio`` is the proportion of frames classified as speech
    by ``webrtcvad``, in ``[0, 1]``.  ``rms_db`` is the chunk-level
    RMS in dBFS from the preprocessing layer.
    """

    speech_ratio: float = 0.0
    rms_db: float = float("-inf")
    speech_frames: int = 0
    total_frames: int = 0


@dataclass(frozen=True, slots=True)
class LivenessResult(InferenceResult):
    """Per-frame liveness / anti-spoofing classification.

    Catches print and screen-replay spoofing specifically — not
    deepfakes or 3D masks (``docs/04-inference-modules-design.md``
    §7).  The single-frame ``is_real`` classification is a point
    estimate; the fusion engine collects consecutive frames within a
    sampling window and computes the multi-frame confidence
    interval, mirroring the identity-match pipeline.

    **Output contract — verified against the real MiniFASNetV2 model
    (turn N+12, the API-call bug-fix pass).** The upstream
    ``uniface.MiniFASNet.predict(image, bbox)`` returns
    ``SpoofingResult(is_real: bool, confidence: float)`` — a
    **2-class** softmax (real/spoof), not the 3-class
    ``(real, print, replay)`` the original design doc hypothesised.
    The model does not distinguish print vs replay in its output; if
    we ever need that distinction, it requires a different model
    family, not a config change.

    The model's softmax confidence on the predicted class is carried
    on the inherited ``InferenceResult.confidence`` field as a
    degenerate point interval (``(score, score, score)``).  The
    fusion engine builds the multi-frame interval from the per-frame
    point estimates, the same shape as identity-match and the other
    modalities.
    """

    is_real: bool = True


@dataclass(frozen=True, slots=True)
class BrowserEventResult(InferenceResult):
    """Result for a DOM-level browser event (deterministic, no model).

    Confidence is always 1.0 — there is no model uncertainty in
    "the tab lost focus".  ``detail`` carries the event-specific
    metadata from the client envelope.
    """

    detail: dict[str, Any] = field(default_factory=dict)
