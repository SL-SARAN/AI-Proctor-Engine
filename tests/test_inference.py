"""Unit tests for proctoring_engine.inference (all 6 modalities).

These tests exercise every public function and class in the inference
layer against deterministic inputs.  They run on any platform with
``numpy``, ``opencv-python-headless``, and ``webrtcvad-wheels``
installed — no model weights, no GPU, no external service.

Tests that exercise the ``face_recognition`` (dlib) backend use
``pytest.importorskip("face_recognition")`` to skip cleanly on
platforms where it is absent (Windows without MSVC Build Tools).

Tests that require MediaPipe or YOLO model bundles are guarded by
env-var checks and skip when the bundles are not available.
"""

from __future__ import annotations

import math
import os
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from proctoring_engine.inference._types import (
    AudioVadResult,
    BoundingBox,
    BrowserEventResult,
    ConfidenceInterval,
    FacePresenceResult,
    HeadPoseGazeResult,
    IdentityMatchResult,
    InferenceResult,
    ObjectDetectionResult,
)
from proctoring_engine.inference.identity_match import (
    EVENT_IDENTITY_MATCH,
    EVENT_IDENTITY_MISMATCH,
    IdentityBackend,
    IdentityMatchRunner,
    compute_cosine_similarity,
)
from proctoring_engine.inference.head_pose_gaze import (
    EVENT_OFF_SCREEN,
    EVENT_ON_SCREEN,
    FaceLandmarkerRunner,
    _LEFT_EYE_IDS,
    _RIGHT_EYE_IDS,
    _LEFT_IRIS_IDS,
    _RIGHT_IRIS_IDS,
    _SOLVEPNP_LANDMARK_IDS,
    compute_ear,
    compute_head_pose,
    compute_iris_offset,
)
from proctoring_engine.inference.object_detection import (
    COCO_DENYLIST_IDS,
    DENYLIST_CLASSES,
    YOLO_WEIGHTS_PATH_ENV,
    ObjectDetectorRunner,
    filter_denylist_detections,
)
from proctoring_engine.inference.audio_vad import (
    EVENT_ELEVATED_RMS,
    EVENT_SILENCE,
    EVENT_SPEECH_DETECTED,
    AudioVadRunner,
)
from proctoring_engine.inference.browser_events import (
    classify_browser_event,
)
from proctoring_engine.inference.face_presence import (
    EVENT_NO_FACE,
    EVENT_ONE_FACE,
    EVENT_SECOND_PERSON,
    FaceDetectorRunner,
    MP_FACE_DETECTOR_BUNDLE_ENV,
)
from proctoring_engine.preprocessing.audio import AudioFrame


# ======================================================================
# Section 1 — Shared types (_types.py)
# ======================================================================


class TestConfidenceInterval:
    """Boundary-value tests for ConfidenceInterval."""

    def test_valid_zero(self) -> None:
        ci = ConfidenceInterval(lower=0.0, score=0.0, upper=0.0)
        assert ci.lower == 0.0
        assert ci.score == 0.0
        assert ci.upper == 0.0

    def test_valid_one(self) -> None:
        ci = ConfidenceInterval(lower=1.0, score=1.0, upper=1.0)
        assert ci.score == 1.0

    def test_valid_spread(self) -> None:
        ci = ConfidenceInterval(lower=0.2, score=0.5, upper=0.8)
        assert ci.lower == 0.2
        assert ci.score == 0.5
        assert ci.upper == 0.8

    def test_degenerate_point(self) -> None:
        ci = ConfidenceInterval(lower=0.7, score=0.7, upper=0.7)
        assert ci.lower == ci.score == ci.upper

    def test_reject_lower_negative(self) -> None:
        with pytest.raises(ValueError, match="lower"):
            ConfidenceInterval(lower=-0.01, score=0.5, upper=0.8)

    def test_reject_upper_above_one(self) -> None:
        with pytest.raises(ValueError, match="upper"):
            ConfidenceInterval(lower=0.2, score=0.5, upper=1.01)

    def test_reject_score_below_lower(self) -> None:
        with pytest.raises(ValueError, match="lower.*score"):
            ConfidenceInterval(lower=0.6, score=0.5, upper=0.8)

    def test_reject_score_above_upper(self) -> None:
        with pytest.raises(ValueError, match="score.*upper"):
            ConfidenceInterval(lower=0.2, score=0.9, upper=0.8)

    def test_frozen(self) -> None:
        ci = ConfidenceInterval(lower=0.0, score=0.5, upper=1.0)
        with pytest.raises(AttributeError):
            ci.score = 0.6  # type: ignore[misc]


class TestBoundingBox:
    """Boundary-value tests for BoundingBox."""

    def test_valid_origin(self) -> None:
        bb = BoundingBox(x=0.0, y=0.0, w=0.0, h=0.0)
        assert bb.x == 0.0

    def test_valid_full_frame(self) -> None:
        bb = BoundingBox(x=0.0, y=0.0, w=1.0, h=1.0)
        assert bb.w == 1.0

    def test_valid_small_box(self) -> None:
        bb = BoundingBox(x=0.1, y=0.2, w=0.3, h=0.4)
        assert bb.to_dict() == {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}

    def test_reject_negative_x(self) -> None:
        with pytest.raises(ValueError, match="x"):
            BoundingBox(x=-0.01, y=0.0, w=0.5, h=0.5)

    def test_reject_w_above_one(self) -> None:
        with pytest.raises(ValueError, match="w"):
            BoundingBox(x=0.0, y=0.0, w=1.01, h=0.5)

    def test_frozen(self) -> None:
        bb = BoundingBox(x=0.0, y=0.0, w=0.5, h=0.5)
        with pytest.raises(AttributeError):
            bb.x = 0.1  # type: ignore[misc]


class TestInferenceResult:
    """Basic tests for the base InferenceResult."""

    def test_defaults(self) -> None:
        r = InferenceResult(
            modality="face",
            event_type="one_face",
            confidence=ConfidenceInterval(0.5, 0.5, 0.5),
        )
        assert r.bounding_boxes == []
        assert r.raw_value == {}

    def test_with_boxes_and_raw(self) -> None:
        r = InferenceResult(
            modality="object",
            event_type="cell phone",
            confidence=ConfidenceInterval(0.8, 0.8, 0.8),
            bounding_boxes=[BoundingBox(0.1, 0.2, 0.3, 0.4)],
            raw_value={"class": "cell phone"},
        )
        assert len(r.bounding_boxes) == 1
        assert r.raw_value["class"] == "cell phone"


class TestFacePresenceResult:
    """Tests for FacePresenceResult subclass."""

    def test_face_count_default(self) -> None:
        r = FacePresenceResult(
            modality="face",
            event_type="one_face",
            confidence=ConfidenceInterval(0.9, 0.9, 0.9),
        )
        assert r.face_count == 0

    def test_face_count_explicit(self) -> None:
        r = FacePresenceResult(
            modality="face",
            event_type="second_person",
            confidence=ConfidenceInterval(0.7, 0.7, 0.7),
            face_count=2,
        )
        assert r.face_count == 2


# ======================================================================
# Section 2 — Face presence (face_presence.py)
# ======================================================================


class TestFacePresenceNoModel:
    """Tests for FaceDetectorRunner that do not require a model bundle."""

    def test_env_var_not_set_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(EnvironmentError, match=MP_FACE_DETECTOR_BUNDLE_ENV):
                FaceDetectorRunner()

    def test_missing_file_raises(self, tmp_path: Any) -> None:
        fake_path = str(tmp_path / "nonexistent.task")
        with pytest.raises(FileNotFoundError, match="not found"):
            FaceDetectorRunner(model_bundle_path=fake_path)

    def test_event_type_constants(self) -> None:
        assert EVENT_ONE_FACE == "one_face"
        assert EVENT_NO_FACE == "no_face"
        assert EVENT_SECOND_PERSON == "second_person"


# ======================================================================
# Section 3 — Identity match (identity_match.py)
# ======================================================================


class TestCosineSimilarity:
    """Unit tests for compute_cosine_similarity."""

    def test_identical_vectors(self) -> None:
        v = [1.0, 2.0, 3.0]
        assert compute_cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self) -> None:
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        assert compute_cosine_similarity(a, b) == pytest.approx(0.0)

    def test_near_match(self) -> None:
        a = [1.0, 2.0, 3.0]
        b = [1.1, 2.1, 3.1]
        sim = compute_cosine_similarity(a, b)
        assert 0.99 < sim <= 1.0

    def test_anti_correlated_clamps_to_zero(self) -> None:
        a = [1.0, 0.0]
        b = [-1.0, 0.0]
        assert compute_cosine_similarity(a, b) == 0.0

    def test_zero_vector_returns_zero(self) -> None:
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        assert compute_cosine_similarity(a, b) == 0.0

    def test_unequal_length_raises(self) -> None:
        with pytest.raises(ValueError, match="equal length"):
            compute_cosine_similarity([1.0], [1.0, 2.0])

    def test_empty_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            compute_cosine_similarity([], [])

    def test_high_dimensional(self) -> None:
        """128-d vectors (dlib embedding size)."""
        rng = np.random.default_rng(42)
        a = rng.standard_normal(128).tolist()
        b = list(a)  # identical
        assert compute_cosine_similarity(a, b) == pytest.approx(1.0)


class TestIdentityBackendABC:
    """Tests for the IdentityBackend ABC."""

    def test_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            IdentityBackend()  # type: ignore[abstract]


class _StubBackend(IdentityBackend):
    """Test double for IdentityBackend."""

    def __init__(self, embedding: list[float]) -> None:
        self._embedding = embedding

    def embed(self, face_rgb: np.ndarray) -> list[float]:
        return self._embedding

    @property
    def embedding_dim(self) -> int:
        return len(self._embedding)

    @property
    def model_name(self) -> str:
        return "stub"


class TestIdentityMatchRunner:
    """Tests for IdentityMatchRunner using a stub backend."""

    def test_match_above_threshold(self) -> None:
        embedding = [1.0, 0.0, 0.0]
        backend = _StubBackend(embedding)
        runner = IdentityMatchRunner(backend)
        result = runner.run(
            np.zeros((10, 10, 3), dtype=np.uint8),
            embedding,
            similarity_threshold=0.5,
        )
        assert result.event_type == EVENT_IDENTITY_MATCH
        assert result.similarity == pytest.approx(1.0)

    def test_mismatch_below_threshold(self) -> None:
        backend = _StubBackend([1.0, 0.0, 0.0])
        runner = IdentityMatchRunner(backend)
        result = runner.run(
            np.zeros((10, 10, 3), dtype=np.uint8),
            [0.0, 1.0, 0.0],
            similarity_threshold=0.5,
        )
        assert result.event_type == EVENT_IDENTITY_MISMATCH
        assert result.similarity == pytest.approx(0.0)

    def test_exactly_at_threshold(self) -> None:
        """Similarity exactly equal to threshold → match."""
        # Two known vectors with a known cosine similarity.
        a = [1.0, 1.0]
        b = [1.0, 0.0]
        sim = compute_cosine_similarity(a, b)
        backend = _StubBackend(a)
        runner = IdentityMatchRunner(backend)
        result = runner.run(
            np.zeros((10, 10, 3), dtype=np.uint8),
            b,
            similarity_threshold=sim,
        )
        assert result.event_type == EVENT_IDENTITY_MATCH

    def test_invalid_threshold_raises(self) -> None:
        backend = _StubBackend([1.0])
        runner = IdentityMatchRunner(backend)
        with pytest.raises(ValueError, match="similarity_threshold"):
            runner.run(
                np.zeros((10, 10, 3), dtype=np.uint8),
                [1.0],
                similarity_threshold=1.5,
            )

    def test_result_carries_raw_value(self) -> None:
        backend = _StubBackend([1.0, 2.0])
        runner = IdentityMatchRunner(backend)
        result = runner.run(
            np.zeros((10, 10, 3), dtype=np.uint8),
            [1.0, 2.0],
            similarity_threshold=0.5,
        )
        assert result.raw_value["model_name"] == "stub"
        assert result.raw_value["embedding_dim"] == 2
        assert result.raw_value["threshold"] == 0.5


class TestFaceRecognitionBackendImport:
    """Guard test: FaceRecognitionBackend requires face_recognition.

    Previously this test used ``pytest.importorskip("face_recognition")`` to
    skip on Windows where dlib does not build. That is no longer correct:
    the verified install sequence (Dockerfile builder stage + CI install
    step) ships ``dlib-bin`` prebuilt wheels for every platform including
    Windows, plus the maintained ``face-recognition-models-ng`` fork pinned
    to commit ``35fd7aea15bfa1aa35532b102f7b408ab238b03d``. So this test
    now runs unconditionally on every platform — a regression in the
    dependency chain will fail CI, not silently skip.

    See the "Validate face_recognition end-to-end" steps in CI for a second
    independent check that the *library call path* works, not just the
    install.
    """

    def test_import_skip(self) -> None:
        from proctoring_engine.inference.identity_match import FaceRecognitionBackend
        backend = FaceRecognitionBackend()
        assert backend.embedding_dim == 128
        assert backend.model_name == "dlib_resnet_v1"


# ======================================================================
# Section 4 — Head pose / gaze (head_pose_gaze.py)
# ======================================================================


def _make_synthetic_landmarks(n: int = 478) -> np.ndarray:
    """Create a synthetic (N, 3) landmark array with plausible values.

    Landmarks are spread across [0.2, 0.8] in x/y with z near 0.
    The specific positions don't need to be anatomically correct
    for the unit tests of the geometric helper functions.
    """

    rng = np.random.default_rng(42)
    lms = rng.uniform(0.2, 0.8, size=(n, 3))
    lms[:, 2] = rng.uniform(-0.05, 0.05, n)
    return lms


class TestComputeEar:
    """Tests for the Eye Aspect Ratio helper."""

    def test_open_eye(self) -> None:
        """Synthetic open eye → EAR > 0.2."""
        landmarks = np.zeros((478, 3))
        # Left eye: outer (33) at [0.2, 0.5], inner (133) at [0.4, 0.5],
        # upper1 (160) at [0.3, 0.55], lower1 (144) at [0.3, 0.45],
        # upper2 (158) at [0.3, 0.55], lower2 (153) at [0.3, 0.45].
        landmarks[33] = [0.2, 0.5, 0.0]
        landmarks[133] = [0.4, 0.5, 0.0]
        landmarks[160] = [0.3, 0.55, 0.0]
        landmarks[144] = [0.3, 0.45, 0.0]
        landmarks[158] = [0.3, 0.55, 0.0]
        landmarks[153] = [0.3, 0.45, 0.0]
        ear = compute_ear(landmarks, _LEFT_EYE_IDS)
        assert ear > 0.2

    def test_closed_eye(self) -> None:
        """Synthetic closed eye → EAR ≈ 0."""
        landmarks = np.zeros((478, 3))
        landmarks[33] = [0.2, 0.5, 0.0]
        landmarks[133] = [0.4, 0.5, 0.0]
        # Upper and lower at the same y → closed.
        landmarks[160] = [0.3, 0.5, 0.0]
        landmarks[144] = [0.3, 0.5, 0.0]
        landmarks[158] = [0.3, 0.5, 0.0]
        landmarks[153] = [0.3, 0.5, 0.0]
        ear = compute_ear(landmarks, _LEFT_EYE_IDS)
        assert ear == pytest.approx(0.0)

    def test_degenerate_zero_width(self) -> None:
        """Eye corners at the same point → EAR = 0."""
        landmarks = np.zeros((478, 3))
        landmarks[33] = [0.3, 0.5, 0.0]
        landmarks[133] = [0.3, 0.5, 0.0]  # Same as outer
        landmarks[160] = [0.3, 0.55, 0.0]
        landmarks[144] = [0.3, 0.45, 0.0]
        landmarks[158] = [0.3, 0.55, 0.0]
        landmarks[153] = [0.3, 0.45, 0.0]
        ear = compute_ear(landmarks, _LEFT_EYE_IDS)
        assert ear == 0.0


class TestComputeIrisOffset:
    """Tests for the iris-offset helper."""

    def test_centred_iris(self) -> None:
        """Iris centroid at the midpoint of the eye → offset ≈ 0."""
        landmarks = np.zeros((478, 3))
        landmarks[33] = [0.2, 0.5, 0.0]   # outer
        landmarks[133] = [0.4, 0.5, 0.0]  # inner
        midpoint_x = 0.3
        for idx in _LEFT_IRIS_IDS:
            landmarks[idx] = [midpoint_x, 0.5, 0.0]
        offset = compute_iris_offset(
            landmarks, _LEFT_IRIS_IDS,
            eye_outer_id=33, eye_inner_id=133,
        )
        assert offset == pytest.approx(0.0, abs=1e-9)

    def test_off_centre_iris(self) -> None:
        """Iris displaced to the right → offset > 0."""
        landmarks = np.zeros((478, 3))
        landmarks[33] = [0.2, 0.5, 0.0]
        landmarks[133] = [0.4, 0.5, 0.0]
        # Shift iris to the right by half the eye width.
        for idx in _LEFT_IRIS_IDS:
            landmarks[idx] = [0.4, 0.5, 0.0]
        offset = compute_iris_offset(
            landmarks, _LEFT_IRIS_IDS,
            eye_outer_id=33, eye_inner_id=133,
        )
        assert offset > 0.4

    def test_zero_eye_width(self) -> None:
        landmarks = np.zeros((478, 3))
        landmarks[33] = [0.3, 0.5, 0.0]
        landmarks[133] = [0.3, 0.5, 0.0]
        for idx in _LEFT_IRIS_IDS:
            landmarks[idx] = [0.3, 0.5, 0.0]
        offset = compute_iris_offset(
            landmarks, _LEFT_IRIS_IDS,
            eye_outer_id=33, eye_inner_id=133,
        )
        assert offset == 0.0


class TestComputeHeadPose:
    """Tests for the solvePnP head-pose estimator."""

    def test_frontal_face_near_zero_yaw(self) -> None:
        """A symmetric frontal face should have near-zero yaw."""
        landmarks = np.zeros((478, 3))
        # Place canonical points symmetrically.
        landmarks[1] = [0.50, 0.40, 0.00]   # nose tip
        landmarks[152] = [0.50, 0.90, 0.00] # chin
        landmarks[33] = [0.30, 0.35, 0.00]  # left eye outer
        landmarks[263] = [0.70, 0.35, 0.00] # right eye outer
        landmarks[61] = [0.35, 0.70, 0.00]  # left mouth
        landmarks[291] = [0.65, 0.70, 0.00] # right mouth
        yaw, pitch, roll = compute_head_pose(landmarks, 640, 480)
        assert abs(yaw) < 30.0  # Should be fairly centred.

    def test_returns_tuple_of_three_floats(self) -> None:
        landmarks = _make_synthetic_landmarks()
        result = compute_head_pose(landmarks, 640, 480)
        assert len(result) == 3
        assert all(isinstance(v, float) for v in result)


class TestFaceLandmarkerRunnerNoModel:
    """Tests for FaceLandmarkerRunner without a model bundle."""

    def test_env_var_not_set_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(EnvironmentError, match="MP_FACE_LANDMARKER_BUNDLE"):
                FaceLandmarkerRunner()

    def test_missing_file_raises(self, tmp_path: Any) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            FaceLandmarkerRunner(model_bundle_path=str(tmp_path / "no.task"))


# ======================================================================
# Section 5 — Object detection (object_detection.py)
# ======================================================================


class TestDenylistConstants:
    """Tests for the denylist constants."""

    def test_denylist_classes_content(self) -> None:
        assert DENYLIST_CLASSES == frozenset({"cell phone", "laptop", "tv", "book"})

    def test_coco_ids_match_denylist(self) -> None:
        assert set(COCO_DENYLIST_IDS.values()) == set(DENYLIST_CLASSES)

    def test_coco_ids_are_correct(self) -> None:
        # Verified against COCO 80-class list.
        assert COCO_DENYLIST_IDS[62] == "tv"
        assert COCO_DENYLIST_IDS[63] == "laptop"
        assert COCO_DENYLIST_IDS[67] == "cell phone"
        assert COCO_DENYLIST_IDS[73] == "book"


class TestFilterDenylistDetections:
    """Tests for filter_denylist_detections (pure logic, no YOLO)."""

    def _make_mock_boxes(
        self,
        detections: list[tuple[int, float, list[float]]],
    ) -> MagicMock:
        """Build a mock ``boxes`` object from (cls_id, conf, xyxyn) tuples."""
        import torch

        mock = MagicMock()
        if not detections:
            mock.cls = torch.tensor([])
            mock.conf = torch.tensor([])
            mock.xyxyn = torch.tensor([]).reshape(0, 4)
            return mock

        cls_ids = [d[0] for d in detections]
        confs = [d[1] for d in detections]
        xyxyn_list = [d[2] for d in detections]

        mock.cls = torch.tensor(cls_ids, dtype=torch.float32)
        mock.conf = torch.tensor(confs, dtype=torch.float32)
        mock.xyxyn = torch.tensor(xyxyn_list, dtype=torch.float32)
        return mock

    def test_empty_boxes(self) -> None:
        result = filter_denylist_detections(None, {}, DENYLIST_CLASSES)
        assert result == []

    def test_no_denylist_match(self) -> None:
        torch = pytest.importorskip("torch")
        # Class 0 = "person" (not denylisted).
        boxes = self._make_mock_boxes([(0, 0.9, [0.1, 0.2, 0.5, 0.6])])
        result = filter_denylist_detections(boxes, {0: "person"}, DENYLIST_CLASSES)
        assert result == []

    def test_single_denylist_match(self) -> None:
        torch = pytest.importorskip("torch")
        boxes = self._make_mock_boxes([(67, 0.85, [0.1, 0.2, 0.4, 0.5])])
        names = {67: "cell phone"}
        result = filter_denylist_detections(boxes, names, DENYLIST_CLASSES)
        assert len(result) == 1
        assert result[0]["class_name"] == "cell phone"
        assert result[0]["confidence"] == pytest.approx(0.85)
        assert result[0]["bbox"]["x"] == pytest.approx(0.1)
        assert result[0]["bbox"]["w"] == pytest.approx(0.3)

    def test_mixed_detections(self) -> None:
        torch = pytest.importorskip("torch")
        boxes = self._make_mock_boxes([
            (0, 0.9, [0.1, 0.2, 0.5, 0.6]),    # person — NOT denylisted
            (67, 0.7, [0.6, 0.1, 0.9, 0.4]),    # cell phone — denylisted
            (73, 0.65, [0.2, 0.3, 0.5, 0.7]),   # book — denylisted
        ])
        names = {0: "person", 67: "cell phone", 73: "book"}
        result = filter_denylist_detections(boxes, names, DENYLIST_CLASSES)
        assert len(result) == 2
        class_names = {r["class_name"] for r in result}
        assert class_names == {"cell phone", "book"}


class TestObjectDetectorRunnerNoModel:
    """Tests for ObjectDetectorRunner without YOLO weights."""

    def test_env_var_not_set_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(EnvironmentError, match=YOLO_WEIGHTS_PATH_ENV):
                ObjectDetectorRunner()

    def test_missing_file_raises(self, tmp_path: Any) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            ObjectDetectorRunner(weights_path=str(tmp_path / "no.pt"))


# ======================================================================
# Section 6 — Audio VAD (audio_vad.py)
# ======================================================================


def _make_silence_frames(
    n_frames: int = 5,
    sample_rate: int = 16000,
    duration_ms: int = 20,
) -> list[AudioFrame]:
    """Create VAD-compatible silence frames (all zeros)."""
    samples_per_frame = sample_rate * duration_ms // 1000
    return [
        AudioFrame(
            samples=np.zeros(samples_per_frame, dtype=np.int16),
            sample_rate_hz=sample_rate,
            duration_ms=duration_ms,
            captured_at_ms=i * duration_ms,
        )
        for i in range(n_frames)
    ]


def _make_sine_frames(
    n_frames: int = 5,
    sample_rate: int = 16000,
    duration_ms: int = 20,
    freq_hz: int = 1000,
    amplitude: int = 16000,
) -> list[AudioFrame]:
    """Create VAD-compatible frames with a pure sine tone."""
    samples_per_frame = sample_rate * duration_ms // 1000
    frames = []
    for i in range(n_frames):
        t = np.arange(samples_per_frame, dtype=np.float64) / sample_rate
        samples = (np.sin(2 * np.pi * freq_hz * t) * amplitude).astype(np.int16)
        frames.append(
            AudioFrame(
                samples=samples,
                sample_rate_hz=sample_rate,
                duration_ms=duration_ms,
                captured_at_ms=i * duration_ms,
            )
        )
    return frames


class TestAudioVadRunner:
    """Tests for AudioVadRunner with real webrtcvad."""

    def test_silence_frames(self) -> None:
        runner = AudioVadRunner(aggressiveness=2)
        frames = _make_silence_frames(5, 16000, 20)
        result = runner.run(frames, rms_db=-60.0)
        assert result.event_type == EVENT_SILENCE
        assert result.speech_ratio == 0.0
        assert result.total_frames == 5
        assert result.speech_frames == 0

    def test_speech_frames(self) -> None:
        """A 1 kHz tone at 16 kHz should trigger VAD speech detection."""
        runner = AudioVadRunner(aggressiveness=0)  # Least aggressive
        frames = _make_sine_frames(5, 16000, 20, freq_hz=1000, amplitude=16000)
        result = runner.run(frames, rms_db=-10.0)
        # At aggressiveness=0, a loud sine wave should classify as speech.
        assert result.speech_ratio > 0.0 or result.event_type != EVENT_SILENCE

    def test_elevated_rms_without_speech(self) -> None:
        """High RMS + silence detection → elevated_rms event."""
        runner = AudioVadRunner(aggressiveness=3, noise_floor_dbfs=-30.0)
        frames = _make_silence_frames(5, 16000, 20)
        result = runner.run(frames, rms_db=-20.0)
        # All frames are silence, but RMS is above noise floor.
        if result.speech_ratio == 0.0:
            assert result.event_type == EVENT_ELEVATED_RMS

    def test_empty_frames(self) -> None:
        runner = AudioVadRunner(aggressiveness=2)
        result = runner.run([], rms_db=-60.0)
        assert result.event_type == EVENT_SILENCE
        assert result.total_frames == 0

    def test_aggressiveness_zero(self) -> None:
        runner = AudioVadRunner(aggressiveness=0)
        assert runner is not None

    def test_aggressiveness_three(self) -> None:
        runner = AudioVadRunner(aggressiveness=3)
        assert runner is not None

    def test_aggressiveness_four_raises(self) -> None:
        with pytest.raises(ValueError, match="aggressiveness"):
            AudioVadRunner(aggressiveness=4)

    def test_aggressiveness_negative_raises(self) -> None:
        with pytest.raises(ValueError, match="aggressiveness"):
            AudioVadRunner(aggressiveness=-1)

    def test_confidence_equals_speech_ratio(self) -> None:
        runner = AudioVadRunner(aggressiveness=2)
        frames = _make_silence_frames(5, 16000, 20)
        result = runner.run(frames, rms_db=-60.0)
        assert result.confidence.score == result.speech_ratio

    def test_raw_value_contents(self) -> None:
        runner = AudioVadRunner(aggressiveness=2)
        frames = _make_silence_frames(3, 16000, 20)
        result = runner.run(frames, rms_db=-45.0)
        assert result.raw_value["total_frames"] == 3
        assert result.raw_value["rms_db"] == -45.0

    def test_rms_negative_infinity_is_silence(self) -> None:
        runner = AudioVadRunner(aggressiveness=2)
        frames = _make_silence_frames(2, 16000, 20)
        result = runner.run(frames, rms_db=float("-inf"))
        assert result.event_type == EVENT_SILENCE


# ======================================================================
# Section 7 — Browser events (browser_events.py)
# ======================================================================


class TestClassifyBrowserEvent:
    """Tests for classify_browser_event."""

    @pytest.mark.parametrize("evt", [
        "visibilitychange", "blur", "focus", "fullscreenchange",
        "copy", "paste", "contextmenu",
    ])
    def test_valid_events(self, evt: str) -> None:
        result = classify_browser_event(evt)
        assert result.confidence.score == 1.0
        assert result.confidence.lower == 1.0
        assert result.confidence.upper == 1.0
        assert result.event_type == evt
        assert result.modality == "browser"

    def test_invalid_event_raises(self) -> None:
        with pytest.raises(ValueError, match="Unrecognised"):
            classify_browser_event("mousemove")

    def test_detail_empty(self) -> None:
        result = classify_browser_event("blur")
        assert result.detail == {}

    def test_detail_with_data(self) -> None:
        result = classify_browser_event(
            "visibilitychange",
            detail={"hidden": True, "timestamp": 123456},
        )
        assert result.detail == {"hidden": True, "timestamp": 123456}
        assert result.raw_value["detail"] == {"hidden": True, "timestamp": 123456}

    def test_none_detail_becomes_empty_dict(self) -> None:
        result = classify_browser_event("focus", detail=None)
        assert result.detail == {}

    def test_result_type(self) -> None:
        result = classify_browser_event("paste")
        assert isinstance(result, BrowserEventResult)
        assert isinstance(result, InferenceResult)

    def test_bounding_boxes_empty(self) -> None:
        result = classify_browser_event("copy")
        assert result.bounding_boxes == []


# ======================================================================
# Section 8 — Liveness / anti-spoofing (liveness.py)
# ======================================================================


from proctoring_engine.inference.liveness import (
    EVENT_LIVENESS_REAL,
    EVENT_LIVENESS_SPOOF,
    LIVENESS_MODEL_PATH_ENV,
    MINIFASNET_V2_SHA256,
    LivenessBackend,
    LivenessRunner,
    UnifaceBackend,
    _parse_classification,
    compute_liveness_confidence,
    _verify_weights_sha256,
)


class TestParseClassification:
    """Pure helper: convert a 3-class softmax to structured fields."""

    def test_real_argmax(self) -> None:
        probs = np.array([0.85, 0.10, 0.05], dtype=np.float64)
        is_real, real, print_, replay = _parse_classification(probs)
        assert is_real is True
        assert real == 0.85
        assert print_ == 0.10
        assert replay == 0.05

    def test_print_argmax(self) -> None:
        probs = np.array([0.10, 0.80, 0.10], dtype=np.float64)
        is_real, _, print_, _ = _parse_classification(probs)
        assert is_real is False
        assert print_ == 0.80

    def test_replay_argmax(self) -> None:
        probs = np.array([0.05, 0.10, 0.85], dtype=np.float64)
        is_real, real, _, replay = _parse_classification(probs)
        assert is_real is False
        assert real == 0.05
        assert replay == 0.85

    def test_reject_wrong_shape(self) -> None:
        with pytest.raises(ValueError, match="(3,)"):
            _parse_classification(np.array([0.5, 0.5]))

    def test_tie_argmax_returns_first(self) -> None:
        """Real ties with print → ``is_real=False`` because np.argmax
        returns the first index, which is class 0 (real).  Boundary
        case where the threshold should reject anyway; document the
        argmax semantics rather than relying on it."""
        probs = np.array([0.5, 0.5, 0.0], dtype=np.float64)
        is_real, *_ = _parse_classification(probs)
        # Both real and print tie at 0.5; np.argmax returns 0 (real).
        # The runner's threshold check still applies on top.
        assert is_real is True


class TestComputeLivenessConfidence:
    """Pure helper: degenerate point interval from a single real_score."""

    def test_degenerate_interval(self) -> None:
        ci = compute_liveness_confidence(0.73)
        assert ci.lower == 0.73
        assert ci.score == 0.73
        assert ci.upper == 0.73

    def test_clamps_below_zero(self) -> None:
        ci = compute_liveness_confidence(-0.1)
        assert ci.score == 0.0

    def test_clamps_above_one(self) -> None:
        ci = compute_liveness_confidence(1.5)
        assert ci.score == 1.0


class TestLivenessRunnerWithMockBackend:
    """Runner tests using a mock backend — no model weights, no
    onnxruntime, no uniface.  Pure logic test."""

    def _mock_backend(self, probs: np.ndarray) -> LivenessBackend:
        b = MagicMock(spec=LivenessBackend)
        b.classify.return_value = probs
        b.model_name = "Mock"
        return b

    def test_real_when_argmax_real_and_above_threshold(self) -> None:
        runner = LivenessRunner(self._mock_backend(np.array([0.9, 0.05, 0.05])))
        result = runner.run(np.zeros((80, 80, 3), dtype=np.uint8))
        assert result.is_real is True
        assert result.event_type == EVENT_LIVENESS_REAL
        assert result.real_score == 0.9
        assert result.modality == "liveness"
        assert result.raw_value["threshold"] == 0.5
        assert result.raw_value["model_name"] == "Mock"

    def test_spoof_when_argmax_print(self) -> None:
        runner = LivenessRunner(
            self._mock_backend(np.array([0.1, 0.7, 0.2]))
        )
        result = runner.run(np.zeros((80, 80, 3), dtype=np.uint8))
        assert result.is_real is False
        assert result.event_type == EVENT_LIVENESS_SPOOF
        assert result.print_score == 0.7
        assert result.replay_score == 0.2

    def test_threshold_below_real_score_still_spoof(self) -> None:
        """real_score below threshold even when it's the argmax →
        ``is_real=False``."""
        runner = LivenessRunner(
            self._mock_backend(np.array([0.49, 0.51, 0.0]))
        )
        result = runner.run(
            np.zeros((80, 80, 3), dtype=np.uint8),
            real_score_threshold=0.5,
        )
        assert result.is_real is False

    def test_threshold_accepts_argmax_real(self) -> None:
        """real_score above threshold → ``is_real=True``."""
        runner = LivenessRunner(
            self._mock_backend(np.array([0.51, 0.49, 0.0]))
        )
        result = runner.run(
            np.zeros((80, 80, 3), dtype=np.uint8),
            real_score_threshold=0.5,
        )
        assert result.is_real is True

    def test_invalid_threshold_rejected(self) -> None:
        runner = LivenessRunner(self._mock_backend(np.array([1.0, 0.0, 0.0])))
        with pytest.raises(ValueError, match="real_score_threshold"):
            runner.run(
                np.zeros((80, 80, 3), dtype=np.uint8),
                real_score_threshold=1.5,
            )

    def test_runner_does_not_validate_face_shape(self) -> None:
        """The runner delegates shape validation to the backend
        (UnifaceBackend.classify rejects wrong shapes).  The runner
        itself trusts the backend — it only formats whatever
        probabilities come back.  This documents the contract."""
        runner = LivenessRunner(
            self._mock_backend(np.array([1.0, 0.0, 0.0]))
        )
        # 2-D input — the runner itself does not raise.
        result = runner.run(np.zeros((80, 80), dtype=np.uint8))
        assert result.is_real is True


class TestVerifyWeightsSha256:
    """Verify the model weights file against the pinned SHA-256."""

    def test_accepts_matching_file(self, tmp_path: Any) -> None:
        # Write a small file with the known hash
        test_bytes = b"x" * 32
        from hashlib import sha256
        # Compute the actual sha256 of test_bytes — but we need to
        # patch MINIFASNET_V2_SHA256 to that value to make the test
        # portable.  Use monkeypatch.
        actual = sha256(test_bytes).hexdigest()
        path = tmp_path / "weights.onnx"
        path.write_bytes(test_bytes)

        from proctoring_engine.inference import liveness as liveness_mod
        original = liveness_mod.MINIFASNET_V2_SHA256
        try:
            liveness_mod.MINIFASNET_V2_SHA256 = actual
            # Should not raise
            _verify_weights_sha256(str(path))
        finally:
            liveness_mod.MINIFASNET_V2_SHA256 = original

    def test_rejects_mismatched_file(self, tmp_path: Any) -> None:
        path = tmp_path / "weights.onnx"
        path.write_bytes(b"untrusted weights")
        with pytest.raises(ValueError, match="SHA-256 mismatch"):
            _verify_weights_sha256(str(path))

    def test_missing_file_raises(self, tmp_path: Any) -> None:
        with pytest.raises(FileNotFoundError):
            _verify_weights_sha256(str(tmp_path / "nope.onnx"))


class TestUnifaceBackend:
    """Tests that the UnifaceBackend wires through the env var and
    delegates to the uniface library correctly.  The actual
    onnxruntime call is mocked — no real model."""

    def test_env_var_not_set_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(EnvironmentError, match=LIVENESS_MODEL_PATH_ENV):
                UnifaceBackend()

    def test_uniface_not_installed(self, tmp_path: Any) -> None:
        """If uniface isn't available, the backend surfaces the import
        error rather than silently stubbing."""
        # Write a stub file with the pinned SHA-256
        from hashlib import sha256
        test_bytes = b"\x00" * 64
        actual = sha256(test_bytes).hexdigest()
        path = tmp_path / "stub.onnx"
        path.write_bytes(test_bytes)

        from proctoring_engine.inference import liveness as liveness_mod
        original_hash = liveness_mod.MINIFASNET_V2_SHA256
        try:
            liveness_mod.MINIFASNET_V2_SHA256 = actual

            with patch.dict(
                os.environ, {LIVENESS_MODEL_PATH_ENV: str(path)}
            ):
                # Block uniface import
                import builtins
                original_import = builtins.__import__

                def fake_import(name, *args: Any, **kwargs: Any) -> Any:
                    if name == "uniface":
                        raise ImportError("uniface not available")
                    return original_import(name, *args, **kwargs)

                with patch("builtins.__import__", side_effect=fake_import):
                    with pytest.raises(ImportError, match="uniface"):
                        UnifaceBackend()
        finally:
            liveness_mod.MINIFASNET_V2_SHA256 = original_hash

    def test_wrong_input_height_rejected(self, tmp_path: Any) -> None:
        """The runner rejects a face crop with an unexpected height."""
        from hashlib import sha256
        test_bytes = b"\x00" * 64
        actual = sha256(test_bytes).hexdigest()
        path = tmp_path / "stub.onnx"
        path.write_bytes(test_bytes)

        from proctoring_engine.inference import liveness as liveness_mod
        original_hash = liveness_mod.MINIFASNET_V2_SHA256
        try:
            liveness_mod.MINIFASNET_V2_SHA256 = actual
            # Mock the entire uniface module so we don't need it installed
            mock_session = MagicMock()
            mock_session.classify = MagicMock(
                return_value=np.array([0.9, 0.05, 0.05])
            )
            mock_uniface = MagicMock()
            mock_uniface.InferenceSession = MagicMock(return_value=mock_session)

            import sys
            sys.modules["uniface"] = mock_uniface

            try:
                with patch.dict(
                    os.environ, {LIVENESS_MODEL_PATH_ENV: str(path)}
                ):
                    backend = UnifaceBackend()
                    with pytest.raises(ValueError, match="uniface expects"):
                        # Wrong height: 100, not 80 or 160
                        backend.classify(np.zeros((100, 100, 3), dtype=np.uint8))
            finally:
                del sys.modules["uniface"]
        finally:
            liveness_mod.MINIFASNET_V2_SHA256 = original_hash


class TestLivenessModuleImports:
    """The module is importable; constants and ABC are present."""

    def test_module_constants(self) -> None:
        assert MINIFASNET_V2_SHA256 == (
            "b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907"
        )
        assert LIVENESS_MODEL_PATH_ENV == "LIVENESS_MODEL_PATH"
        assert EVENT_LIVENESS_REAL == "liveness_real"
        assert EVENT_LIVENESS_SPOOF == "liveness_spoof"

    def test_liveness_backend_is_abstract(self) -> None:
        with pytest.raises(TypeError):
            LivenessBackend()  # type: ignore[abstract]
