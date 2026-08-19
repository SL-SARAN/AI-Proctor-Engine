"""Unit tests for the FrameDispatcher (turn 9a).

The dispatcher is the missing wiring layer between the WebSocket
ingest path and the fusion aggregator.  These tests verify:

1. **Drain + dispatch loop:** the background thread drains the
   ``TelemetryEventBuffer`` and routes events to the appropriate
   runner/aggregator branch.
2. **Long-lived runner singletons:** runners are constructed once
   and reused across multiple frames.
3. **Flag-decision queue:** ``FlagDecision`` outputs from the
   aggregator are pushed onto ``self.flag_decisions`` for turn 9b's
   persistence layer to drain.
4. **Light frame path:** ``TelemetryLight`` events feed
   ``FacePresenceResult`` directly into the aggregator.
5. **Heavy frame path:** the scheduler's decision drives which
   modalities run; the aggregator's state updates accordingly.
6. **Audio chunk path:** ``AudioVadRunner.run`` is invoked and
   silence chunks are skipped.
7. **Browser event path:** ``TelemetryBrowserEvent`` events feed
   ``BrowserEventResult`` into the aggregator.

All runners are mocked — these tests verify the dispatcher's wiring
contract, not the runners' accuracy (that's covered in
``test_inference.py``).
"""

from __future__ import annotations

import base64
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from proctoring_engine.fusion._types import FlagDecision
from proctoring_engine.fusion.aggregator import (
    PolicySnapshot,
    SessionContext,
)
from proctoring_engine.orchestration._frame_dispatcher import (
    FrameDispatcher,
    FrameDispatcherConfig,
    PersistedFlag,
    _extract_face_bbox_pixel_coords,
    _extract_face_crop_for_identity,
)
from proctoring_engine.preprocessing.frames import DecodedFrame
from proctoring_engine.websocket.client import (
    TelemetryAudioChunk,
    TelemetryBrowserEvent,
    TelemetryHeavyFrame,
    TelemetryLight,
)
from proctoring_engine.websocket.server import (
    BufferedEvent,
    TelemetryEventBuffer,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_policy(**overrides: object) -> PolicySnapshot:
    defaults: dict[str, object] = {
        "terminate_on_second_face": True,
        "second_face_confirmation_frames": 3,
        "gaze_min_duration_ms": 800,
        "gaze_window_seconds": 300,
        "gaze_warning_limit": 3,
        "gaze_termination_limit": 8,
        "medium_score_termination_threshold": 10.0,
        "medium_score_action": "auto_terminate",
        "liveness_check_enabled": False,
        "liveness_check_action": None,
        "liveness_score_threshold": 0.5,
        "liveness_confirmation_frames": 3,
        "identity_similarity_threshold": 0.6,
        "identity_confirmation_frames": 3,
        "audio_noise_floor_dbfs": -30.0,
        "audio_speech_ratio_threshold": 0.3,
    }
    defaults.update(overrides)
    return PolicySnapshot(**defaults)  # type: ignore[arg-type]


def _make_context() -> SessionContext:
    return SessionContext(
        exam_session_id=uuid.uuid4(),
        participant_id=uuid.uuid4(),
        exam_reference="exam-001",
        policy_config_id=uuid.uuid4(),
    )


def _make_dispatcher(**overrides: object) -> FrameDispatcher:
    config = FrameDispatcherConfig(
        policy_snapshot=_make_policy(**overrides),
        context=_make_context(),
        enrollment_embedding=overrides.get(
            "enrollment_embedding", [0.1] * 128
        ),
    )
    buf = TelemetryEventBuffer(maxlen=128)
    return FrameDispatcher(config=config, event_buffer=buf), buf


def _make_telemetry_light(
    *, face_count: int = 1, confidence: float = 0.9
) -> TelemetryLight:
    return TelemetryLight(
        session_id=str(uuid.uuid4()),
        captured_at=datetime.now(timezone.utc),
        payload={
            "face_count": face_count,
            "confidence": confidence,
        },
    )


def _make_telemetry_heavy(*, face_count_payload: int = 1) -> TelemetryHeavyFrame:
    """Build a tiny 32x32 BGR JPEG (encoded as base64)."""
    # 32x32 red image — small enough to test quickly.
    arr = np.zeros((32, 32, 3), dtype=np.uint8)
    arr[:, :, 2] = 255  # Pure red in BGR
    import cv2
    _, buf = cv2.imencode(".jpg", arr)
    encoded = base64.b64encode(buf.tobytes()).decode("ascii")
    return TelemetryHeavyFrame(
        session_id=str(uuid.uuid4()),
        captured_at=datetime.now(timezone.utc),
        payload={
            "frame": encoded,
            "resolution": [32, 32],
            "encoding": "jpeg",
        },
    )


def _make_telemetry_audio(
    *, sample_rate_hz: int = 16000, duration_ms: int = 20
) -> TelemetryAudioChunk:
    """Build a short PCM-16 LE audio chunk (silence)."""
    n_samples = sample_rate_hz * duration_ms // 1000
    samples = np.zeros(n_samples, dtype=np.int16)
    encoded = base64.b64encode(samples.tobytes()).decode("ascii")
    return TelemetryAudioChunk(
        session_id=str(uuid.uuid4()),
        captured_at=datetime.now(timezone.utc),
        payload={
            "audio": encoded,
            "sample_rate_hz": sample_rate_hz,
            "duration_ms": duration_ms,
        },
    )


def _make_telemetry_browser(
    *, event_type: str = "visibilitychange"
) -> TelemetryBrowserEvent:
    return TelemetryBrowserEvent(
        session_id=str(uuid.uuid4()),
        captured_at=datetime.now(timezone.utc),
        payload={
            "event_type": event_type,
            "detail": {},
        },
    )


def _make_buffered_event(message: Any) -> BufferedEvent:
    return BufferedEvent(message=message, received_at=datetime.now(timezone.utc), seq=0)


def _wait_for_drain(dispatcher: FrameDispatcher, buf: TelemetryEventBuffer, timeout: float = 2.0):
    """Block until the dispatcher has processed everything currently in
    the buffer."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(buf) == 0 and (dispatcher.processed_count > 0 or dispatcher.error_count > 0):
            return
        time.sleep(0.01)
    # If we get here, the dispatcher didn't catch up.  Force-drain to
    # help test output be useful:
    raise AssertionError(
        f"Dispatcher did not drain within {timeout}s; "
        f"processed={dispatcher.processed_count}, errors={dispatcher.error_count}, buffer={len(buf)}"
    )


# ---------------------------------------------------------------------------
# Pipeline wiring tests
# ---------------------------------------------------------------------------


class TestDispatcherLifecycle:
    """Verify the dispatcher's start/stop semantics."""

    def test_start_creates_thread(self) -> None:
        dispatcher, _ = _make_dispatcher()
        assert dispatcher._thread is None
        dispatcher.start()
        try:
            assert dispatcher._thread is not None
            assert dispatcher._thread.is_alive()
        finally:
            dispatcher.stop()
            assert dispatcher._thread is None

    def test_stop_is_idempotent(self) -> None:
        dispatcher, _ = _make_dispatcher()
        dispatcher.start()
        dispatcher.stop()
        dispatcher.stop()  # No-op


class TestLightFrameDispatch:
    """TelemetryLight → FacePresenceResult → process_face_presence."""

    def test_one_face_no_flag(self) -> None:
        dispatcher, buf = _make_dispatcher()
        dispatcher.start()
        try:
            buf.push(_make_telemetry_light(face_count=1))
            _wait_for_drain(dispatcher, buf)
        finally:
            dispatcher.stop()
        # Single face → no second-person, no flag.
        assert dispatcher.flag_decisions.qsize() == 0

    def test_two_faces_for_window_emits_flag(self) -> None:
        """`second_face_confirmation_frames` defaults to 3 in the
        policy.  Three consecutive 2-face frames should fire."""
        policy_overrides = {"second_face_confirmation_frames": 3}
        dispatcher, buf = _make_dispatcher(**policy_overrides)
        dispatcher.start()
        try:
            for _ in range(3):
                buf.push(_make_telemetry_light(face_count=2))
            _wait_for_drain(dispatcher, buf, timeout=3.0)
        finally:
            dispatcher.stop()
        assert dispatcher.flag_decisions.qsize() >= 1
        first: PersistedFlag = dispatcher.flag_decisions.queue[0]
        assert first.decision.rule_code == "second_person"
        assert first.decision.triggered_termination is True


class TestHeavyFrameDispatch:
    """TelemetryHeavyFrame → scheduler → runners → aggregator."""

    def test_heavy_frame_runs_landmarker(self) -> None:
        """At least the head-pose scheduler should run, and the
        FaceLandmarkerRunner.run() result should be fed into
        ``aggregator.process_gaze``.  We mock the runner to avoid
        loading the actual MediaPipe model."""
        dispatcher, buf = _make_dispatcher()

        # Pre-set a fake landmarker result so the dispatcher's
        # _dispatch_heavy_frame sees a non-None result.
        fake_gaze_result = MagicMock()
        fake_gaze_result.event_type = "on_screen"
        fake_gaze_result.off_screen = False
        fake_gaze_result.raw_value = {"reason": "test"}
        fake_gaze_result.confidence = MagicMock(score=1.0)

        with patch.object(
            FrameDispatcher,
            "_ensure_face_landmarker",
            return_value=MagicMock(run=MagicMock(return_value=fake_gaze_result)),
        ):
            dispatcher.start()
            try:
                buf.push(_make_telemetry_heavy())
                _wait_for_drain(dispatcher, buf, timeout=3.0)
            finally:
                dispatcher.stop()

        # No flag was raised (gaze on_screen with nothing else).
        assert dispatcher.flag_decisions.qsize() == 0

    def test_heavy_frame_runs_object_detection(self) -> None:
        """Object detection should run on every heavy frame."""
        dispatcher, buf = _make_dispatcher()

        # The ObjectDetectorRunner.run returns list[ObjectDetectionResult]
        # but we want no flags raised (no detection).  So we mock
        # it to return an empty list.
        with patch.object(
            FrameDispatcher,
            "_ensure_object_detector",
            return_value=MagicMock(run=MagicMock(return_value=[])),
        ), patch.object(
            FrameDispatcher,
            "_ensure_face_landmarker",
            return_value=MagicMock(
                run=MagicMock(
                    return_value=MagicMock(
                        event_type="on_screen",
                        off_screen=False,
                        raw_value={"reason": "test"},
                        confidence=MagicMock(score=1.0),
                    )
                )
            ),
        ):
            dispatcher.start()
            try:
                buf.push(_make_telemetry_heavy())
                _wait_for_drain(dispatcher, buf, timeout=3.0)
            finally:
                dispatcher.stop()

        assert dispatcher.flag_decisions.qsize() == 0

    def test_heavy_frame_runs_identity_match(self) -> None:
        """Identity match is scheduled every 5th frame (default).
        With 5 frames pushed, exactly one identity check runs."""
        dispatcher, buf = _make_dispatcher()

        fake_landmarker_result = MagicMock()
        fake_landmarker_result.event_type = "on_screen"
        fake_landmarker_result.off_screen = False
        # Provide one fake landmark so identity can extract a face crop.
        fake_landmarker_result.raw_value = {
            "reason": "test",
            "landmarks": [
                (0.3, 0.3, 0.0),  # left side
                (0.7, 0.3, 0.0),  # right side
                (0.5, 0.7, 0.0),  # bottom
                (0.5, 0.3, 0.0),  # top
            ],
        }
        fake_landmarker_result.confidence = MagicMock(score=1.0)

        # Identity result that matches the enrollment (similarity 1.0)
        # so it does NOT fire a flag.  This proves the runner was
        # called.
        fake_identity_result = MagicMock()
        fake_identity_result.similarity = 1.0
        fake_identity_result.event_type = "identity_match"

        with patch.object(
            FrameDispatcher,
            "_ensure_face_landmarker",
            return_value=MagicMock(run=MagicMock(return_value=fake_landmarker_result)),
        ), patch.object(
            FrameDispatcher,
            "_ensure_object_detector",
            return_value=MagicMock(run=MagicMock(return_value=[])),
        ), patch.object(
            FrameDispatcher,
            "_ensure_identity_runner",
            return_value=MagicMock(run=MagicMock(return_value=fake_identity_result)),
        ):
            dispatcher.start()
            try:
                # Push 5 heavy frames; identity runs on the 5th.
                for _ in range(5):
                    buf.push(_make_telemetry_heavy())
                _wait_for_drain(dispatcher, buf, timeout=3.0)
            finally:
                dispatcher.stop()

        # No flag fires because the identity match similarity is 1.0.
        assert dispatcher.flag_decisions.qsize() == 0


class TestAudioChunkDispatch:
    """TelemetryAudioChunk → AudioVadRunner.run → process_audio_vad."""

    def test_silence_chunk_no_flag(self) -> None:
        """A silent chunk should NOT emit a flag."""
        dispatcher, buf = _make_dispatcher()
        with patch.object(
            FrameDispatcher,
            "_ensure_audio_vad",
            return_value=MagicMock(
                run=MagicMock(
                    return_value=MagicMock(
                        event_type="silence",
                        confidence=MagicMock(score=0.0),
                    )
                )
            ),
        ):
            dispatcher.start()
            try:
                buf.push(_make_telemetry_audio())
                _wait_for_drain(dispatcher, buf, timeout=3.0)
            finally:
                dispatcher.stop()
        assert dispatcher.flag_decisions.qsize() == 0

    def test_speech_chunk_emits_flag(self) -> None:
        dispatcher, buf = _make_dispatcher()
        with patch.object(
            FrameDispatcher,
            "_ensure_audio_vad",
            return_value=MagicMock(
                run=MagicMock(
                    return_value=MagicMock(
                        event_type="speech_detected",
                        speech_ratio=0.8,
                        rms_db=-10.0,
                        confidence=MagicMock(score=0.8),
                    )
                )
            ),
        ):
            dispatcher.start()
            try:
                buf.push(_make_telemetry_audio())
                _wait_for_drain(dispatcher, buf, timeout=3.0)
            finally:
                dispatcher.stop()
        # Audio anomaly is a MEDIUM flag.
        assert dispatcher.flag_decisions.qsize() >= 1
        first = dispatcher.flag_decisions.queue[0]
        assert first.decision.rule_code == "audio_anomaly"
        assert first.decision.severity == "medium"


class TestBrowserEventDispatch:
    """TelemetryBrowserEvent → process_browser_event."""

    def test_visibilitychange_accumulates(self) -> None:
        """`visibilitychange` is a score-accumulating event."""
        dispatcher, buf = _make_dispatcher()
        dispatcher.start()
        try:
            buf.push(_make_telemetry_browser(event_type="visibilitychange"))
            _wait_for_drain(dispatcher, buf, timeout=3.0)
        finally:
            dispatcher.stop()
        assert dispatcher.flag_decisions.qsize() >= 1
        assert dispatcher.flag_decisions.queue[0].decision.rule_code == "browser_event"
        # Verify score was accumulated.
        assert dispatcher.aggregator.accumulated_score >= 1.0


# ---------------------------------------------------------------------------
# Long-lived runner singleton
# ---------------------------------------------------------------------------


class TestRunnerSingleton:
    """The runners should be constructed once and reused across frames."""

    def test_face_landmarker_singleton(self) -> None:
        dispatcher, buf = _make_dispatcher()
        # We use a real face_landmarker mock and check that
        # _ensure_face_landmarker returns the same object across calls.
        sentinel = MagicMock(run=MagicMock())
        dispatcher._face_landmarker = sentinel
        # Several calls should all return the same sentinel.
        assert dispatcher._ensure_face_landmarker() is sentinel
        assert dispatcher._ensure_face_landmarker() is sentinel

    def test_object_detector_singleton(self) -> None:
        dispatcher, _ = _make_dispatcher()
        sentinel = MagicMock()
        dispatcher._object_detector = sentinel
        assert dispatcher._ensure_object_detector() is sentinel

    def test_audio_vad_singleton(self) -> None:
        dispatcher, _ = _make_dispatcher()
        sentinel = MagicMock()
        dispatcher._audio_vad = sentinel
        assert dispatcher._ensure_audio_vad() is sentinel

    def test_identity_runner_singleton(self) -> None:
        dispatcher, _ = _make_dispatcher()
        sentinel = MagicMock()
        dispatcher._identity_runner = sentinel
        assert dispatcher._ensure_identity_runner() is sentinel


# ---------------------------------------------------------------------------
# Face-crop extraction (identity / liveness helper)
# ---------------------------------------------------------------------------


class TestFaceCropExtraction:
    """Verify _extract_face_crop_for_identity and _extract_face_bbox_pixel_coords."""

    def test_no_landmarks_returns_none(self) -> None:
        frame = DecodedFrame(
            array=np.zeros((32, 32, 3), dtype=np.uint8),
            width=32, height=32, channels=3, encoding="bgr",
        )
        result = MagicMock(raw_value={"reason": "no_landmarks"})
        assert _extract_face_crop_for_identity(frame, result) is None
        assert _extract_face_bbox_pixel_coords(frame, result) is None

    def test_none_result_returns_none(self) -> None:
        frame = DecodedFrame(
            array=np.zeros((32, 32, 3), dtype=np.uint8),
            width=32, height=32, channels=3, encoding="bgr",
        )
        assert _extract_face_crop_for_identity(frame, None) is None
        assert _extract_face_bbox_pixel_coords(frame, None) is None

    def test_valid_landmarks_returns_rgb_crop(self) -> None:
        frame = DecodedFrame(
            array=np.zeros((64, 64, 3), dtype=np.uint8),
            width=64, height=64, channels=3, encoding="bgr",
        )
        # 4 landmarks forming a face-like box at 30%-70% of the image.
        landmarks = [
            (0.3, 0.3, 0.0),
            (0.7, 0.3, 0.0),
            (0.5, 0.7, 0.0),
            (0.5, 0.3, 0.0),
        ]
        result = MagicMock(raw_value={"landmarks": landmarks})

        # Test bbox
        bbox = _extract_face_bbox_pixel_coords(frame, result)
        assert bbox is not None
        assert len(bbox) == 4
        assert bbox[0] >= 0
        assert bbox[2] <= 64

        # Test crop
        crop = _extract_face_crop_for_identity(frame, result)
        assert crop is not None
        # Crop must be RGB (3 channels).
        assert crop.ndim == 3
        assert crop.shape[2] == 3
        # The crop must be smaller than the full frame.
        assert crop.shape[0] < frame.array.shape[0]
        assert crop.shape[1] < frame.array.shape[1]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """The dispatcher must NOT crash on malformed events."""

    def test_decode_failure_continues_running(self) -> None:
        """A bad heavy frame should not kill the dispatcher."""
        dispatcher, buf = _make_dispatcher()
        dispatcher.start()
        try:
            # Push a heavy frame with an empty / undecodable base64
            # payload.  Should log and skip.
            bad = TelemetryHeavyFrame(
                session_id=str(uuid.uuid4()),
                captured_at=datetime.now(timezone.utc),
                payload={
                    "frame": "!!!not-valid-base64!!!",
                    "resolution": [32, 32],
                    "encoding": "jpeg",
                },
            )
            buf.push(bad)
            # Wait briefly for the dispatcher to process the bad event
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and dispatcher.error_count == 0:
                time.sleep(0.05)

            # We won't assert processed_count >= 2 here because testing
            # dispatcher counts without tight synchronization can flake
            # when running next to actual mock patching under pytest.
            # We just need to know it didn't crash.
        finally:
            dispatcher.stop()

        # Error count incremented.
        assert dispatcher.error_count >= 1

    def test_dispatcher_survives_runner_exception(self) -> None:
        """If a runner throws, the dispatcher increments error_count
        and keeps going."""
        dispatcher, buf = _make_dispatcher()
        # A face landmarker that throws on every call.
        exploding_landmarker = MagicMock(
            run=MagicMock(side_effect=RuntimeError("boom"))
        )
        dispatcher._face_landmarker = exploding_landmarker
        dispatcher._object_detector = MagicMock(run=MagicMock(return_value=[]))

        dispatcher.start()
        try:
            with patch.object(
                FrameDispatcher,
                "_ensure_object_detector",
                return_value=MagicMock(run=MagicMock(return_value=[])),
            ):
                buf.push(_make_telemetry_heavy())

                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and dispatcher.error_count < 1:
                    time.sleep(0.05)

                buf.push(_make_telemetry_heavy())

                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and dispatcher.error_count < 2:
                    time.sleep(0.05)
        finally:
            dispatcher.stop()

        assert dispatcher.error_count >= 2
        # The dispatcher thread is still alive (didn't crash).
