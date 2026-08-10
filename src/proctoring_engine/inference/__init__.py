"""Inference modules package.

Six server-side modalities that consume decoded, normalised output
from the preprocessing layer and emit typed ``InferenceResult``
objects for the fusion engine.

Each module is a **stateless per-frame classifier** — rolling-window
aggregation, event building (e.g. ``GazeAwayEvent``), and the
flag-raising logic belong to the fusion engine (the next layer).

Modules:

- :mod:`~proctoring_engine.inference.face_presence` — MediaPipe
  ``FaceDetector``, face count + bounding boxes.
- :mod:`~proctoring_engine.inference.identity_match` — face embedding
  + cosine similarity vs enrollment (``face_recognition`` / dlib).
- :mod:`~proctoring_engine.inference.head_pose_gaze` — MediaPipe
  ``FaceLandmarker`` + ``solvePnP`` + EAR + iris offset.
- :mod:`~proctoring_engine.inference.object_detection` — YOLOv8,
  denylist filter.
- :mod:`~proctoring_engine.inference.audio_vad` — ``webrtcvad-wheels``
  VAD + RMS ambient-noise heuristic.
- :mod:`~proctoring_engine.inference.browser_events` — deterministic
  DOM event passthrough.
- :mod:`~proctoring_engine.inference.liveness` — liveness / anti-spoofing
  via ``uniface[cpu]`` (MiniFASNetV2 / ONNX Runtime), catches print and
  screen-replay spoofing.
"""

# --- Shared types ---
from proctoring_engine.inference._types import (
    AudioVadResult,
    BoundingBox,
    BrowserEventResult,
    ConfidenceInterval,
    FacePresenceResult,
    HeadPoseGazeResult,
    IdentityMatchResult,
    InferenceResult,
    LivenessResult,
    ObjectDetectionResult,
)

# --- Face presence ---
from proctoring_engine.inference.face_presence import (
    EVENT_NO_FACE,
    EVENT_ONE_FACE,
    EVENT_SECOND_PERSON,
    FaceDetectorRunner,
    MP_FACE_DETECTOR_BUNDLE_ENV,
)

# --- Identity match ---
from proctoring_engine.inference.identity_match import (
    EVENT_IDENTITY_MATCH,
    EVENT_IDENTITY_MISMATCH,
    FaceRecognitionBackend,
    IdentityBackend,
    IdentityMatchRunner,
    compute_cosine_similarity,
)

# --- Head pose / gaze ---
from proctoring_engine.inference.head_pose_gaze import (
    EVENT_OFF_SCREEN,
    EVENT_ON_SCREEN,
    FaceLandmarkerRunner,
    MP_FACE_LANDMARKER_BUNDLE_ENV,
    compute_ear,
    compute_head_pose,
    compute_iris_offset,
)

# --- Object detection ---
from proctoring_engine.inference.object_detection import (
    COCO_DENYLIST_IDS,
    DENYLIST_CLASSES,
    ObjectDetectorRunner,
    YOLO_WEIGHTS_PATH_ENV,
    filter_denylist_detections,
)

# --- Audio VAD ---
from proctoring_engine.inference.audio_vad import (
    EVENT_ELEVATED_RMS,
    EVENT_SILENCE,
    EVENT_SPEECH_DETECTED,
    AudioVadRunner,
)

# --- Browser events ---
from proctoring_engine.inference.browser_events import (
    classify_browser_event,
)

# --- Liveness / anti-spoofing ---
from proctoring_engine.inference.liveness import (
    EVENT_LIVENESS_REAL,
    EVENT_LIVENESS_SPOOF,
    LIVENESS_MODEL_PATH_ENV,
    LivenessBackend,
    LivenessRunner,
    MINIFASNET_V2_SHA256,
    UnifaceBackend,
    compute_liveness_confidence,
)

__all__ = [
    # Shared types
    "AudioVadResult",
    "BoundingBox",
    "BrowserEventResult",
    "ConfidenceInterval",
    "FacePresenceResult",
    "HeadPoseGazeResult",
    "IdentityMatchResult",
    "InferenceResult",
    "LivenessResult",
    "ObjectDetectionResult",
    # Face presence
    "EVENT_NO_FACE",
    "EVENT_ONE_FACE",
    "EVENT_SECOND_PERSON",
    "FaceDetectorRunner",
    "MP_FACE_DETECTOR_BUNDLE_ENV",
    # Identity match
    "EVENT_IDENTITY_MATCH",
    "EVENT_IDENTITY_MISMATCH",
    "FaceRecognitionBackend",
    "IdentityBackend",
    "IdentityMatchRunner",
    "compute_cosine_similarity",
    # Head pose / gaze
    "EVENT_OFF_SCREEN",
    "EVENT_ON_SCREEN",
    "FaceLandmarkerRunner",
    "MP_FACE_LANDMARKER_BUNDLE_ENV",
    "compute_ear",
    "compute_head_pose",
    "compute_iris_offset",
    # Object detection
    "COCO_DENYLIST_IDS",
    "DENYLIST_CLASSES",
    "ObjectDetectorRunner",
    "YOLO_WEIGHTS_PATH_ENV",
    "filter_denylist_detections",
    # Audio VAD
    "EVENT_ELEVATED_RMS",
    "EVENT_SILENCE",
    "EVENT_SPEECH_DETECTED",
    "AudioVadRunner",
    # Browser events
    "classify_browser_event",
    # Liveness / anti-spoofing
    "EVENT_LIVENESS_REAL",
    "EVENT_LIVENESS_SPOOF",
    "LIVENESS_MODEL_PATH_ENV",
    "LivenessBackend",
    "LivenessRunner",
    "MINIFASNET_V2_SHA256",
    "UnifaceBackend",
    "compute_liveness_confidence",
]
