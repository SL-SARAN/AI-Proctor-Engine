"""FrameDispatcher — the missing wiring layer that turns ingested
telemetry events into inference results and feeds them into the
fusion aggregator.

**Lifecycle:** one dispatcher per active exam session (per WebSocket
connection). Constructed when the WebSocket handshake completes; runs
in the background until the session ends.

**Long-lived runner singletons — explicit design choice.** The
inference runners (FaceLandmarkerRunner, IdentityMatchRunner,
ObjectDetectorRunner, AudioVadRunner) hold heavy model weights in
memory (MediaPipe ``.task`` bundle; ~58 MB onnxruntime; Ultralytics
YOLOv8 weights; dlib weights). Reconstructing them per frame would
defeat the model-loading cost and cause major GC churn under load.
The runners are stateless from the caller's perspective (each
``run()`` call is a single forward pass on a tensor), so reusing one
instance per session is safe.  The dispatcher's runner instances are
kept as private attributes and disposed via ``close()`` when the
session ends.

**Pipeline shape** (per heavy-frame / audio-chunk arrival):

1. Drain the session's ``TelemetryEventBuffer`` (a background loop).
2. For each ``TelemetryHeavyFrame`` event:
   a. Decode the base64 JPEG → ``DecodedFrame`` (BGR via
      ``preprocessing.frames.decode_jpeg_frame``).
   b. Increment the per-session heavy-frame sequence counter.
   c. Ask the ``ModalityScheduler`` which modalities should run this
      frame.
   d. For each modality that runs:
      - ``HEAD_POSE_GAZE`` → ``FaceLandmarkerRunner.run(frame)`` →
        ``aggregator.process_gaze``.
      - ``OBJECT_DETECTION`` → ``ObjectDetectorRunner.run(frame)``
        (which already filters its denylist internally) →
        ``aggregator.process_object_detection``.
      - ``IDENTITY_MATCH`` → reuses the FaceLandmarker's landmarks
        (now exposed in ``raw_value["landmarks"]`` as of Turn 9a)
        to crop a face → ``IdentityMatchRunner.run(...)`` →
        ``aggregator.process_identity_match``.
3. For each ``TelemetryAudioChunk`` event:
   a. Decode the base64 PCM → ``np.ndarray`` of int16 samples + ``rms_db``.
   b. Split into ``AudioFrame`` list.
   c. ``AudioVadRunner.run(frames, rms_db)``.
   d. ``aggregator.process_audio_vad`` (skipped if the chunk was silence).
4. For each ``TelemetryLight`` event (client-side face-presence):
   build a ``FacePresenceResult`` directly → ``aggregator.process_face_presence``.
5. For each ``TelemetryBrowserEvent``: build a ``BrowserEventResult``
   directly → ``aggregator.process_browser_event``.

**Flag persistence:** *not* wired in this turn.  The dispatcher
collects ``FlagDecision`` objects from the aggregator and pushes them
onto ``self.flag_decisions`` (a thread-safe ``queue.Queue``).  Turn 9b
will drain that queue and write ``Flag`` rows to the database.
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import cv2
import numpy as np

from proctoring_engine.fusion.aggregator import (
    FlagDecision,
    PolicySnapshot,
    SessionAggregator,
    SessionContext,
)
from proctoring_engine.inference._types import (
    BrowserEventResult,
    ConfidenceInterval,
    FacePresenceResult,
)
from proctoring_engine.inference.audio_vad import (
    AudioVadRunner,
    EVENT_SILENCE,
)
from proctoring_engine.inference.head_pose_gaze import (
    FaceLandmarkerRunner,
)
from proctoring_engine.inference.identity_match import (
    FaceRecognitionBackend,
    IdentityBackend,
    IdentityMatchRunner,
)
from proctoring_engine.inference.object_detection import (
    DENYLIST_CLASSES,
    ObjectDetectorRunner,
)
from proctoring_engine.preprocessing.audio import (
    split_into_vad_frames,
)
from proctoring_engine.preprocessing.frames import (
    DecodedFrame,
    decode_jpeg_frame,
)
from proctoring_engine.preprocessing.scheduler import (
    InferenceModality,
    ModalityScheduler,
)
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


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FrameDispatcherConfig — constructor arguments
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FrameDispatcherConfig:
    """Configuration injected into ``FrameDispatcher.__init__``.

    Every field maps 1:1 to a ``PolicyConfig`` column or a runner
    constructor argument.  Thresholds live in ``PolicyConfig``, not
    in conditionals.
    """

    policy_snapshot: PolicySnapshot
    context: SessionContext

    # Heavy-frame scheduler periods.
    head_pose_period: int = 1
    object_detection_period: int = 1
    identity_match_period: int = 5

    # The enrollment embedding for identity match.  None disables
    # identity-match (the dispatcher still wires the aggregator
    # branch but emits no decisions until a real embedding is
    # provided).
    enrollment_embedding: list[float] | None = None


# ---------------------------------------------------------------------------
# FrameDispatcher
# ---------------------------------------------------------------------------


class FrameDispatcher:
    """Background-task runner for one exam session.

    The dispatcher owns:
    - One ``SessionAggregator`` instance (per-session state).
    - One ``ModalityScheduler`` instance (per-session schedule).
    - Long-lived inference runners (face landmarker, identity match,
      object detector, audio vad).  Each is constructed lazily on
      first use (heavy model load) and disposed via ``close()`` when
      the session ends.
    - A ``TelemetryEventBuffer`` reference (per-session).
    - A ``queue.Queue`` of emitted ``FlagDecision``s — the persistence
      layer (Turn 9b) will drain this.

    **Thread model.** The dispatcher runs in a background thread that
    drains the WebSocket handler's ``TelemetryEventBuffer``.  The WS
    handler pushes events into the buffer (which is thread-safe); the
    dispatcher's background thread calls ``drain()`` and processes the
    returned batch.  ``FlagDecision`` outputs are pushed onto a
    thread-safe queue.

    **Termination.** ``stop()`` flips a flag the background loop
    checks at the top of each iteration, then joins the thread.
    """

    def __init__(
        self,
        *,
        config: FrameDispatcherConfig,
        event_buffer: TelemetryEventBuffer,
    ) -> None:
        self._config = config
        self._buffer = event_buffer

        # Aggregator + scheduler
        self.aggregator = SessionAggregator(
            policy=config.policy_snapshot,
            context=config.context,
        )
        self._scheduler = ModalityScheduler(
            head_pose_period=config.head_pose_period,
            object_detection_period=config.object_detection_period,
            identity_match_period=config.identity_match_period,
        )

        # Heavy-frame sequence counter (incremented on each heavy
        # frame arrival; drives the scheduler's frame_seq parameter).
        self._heavy_frame_seq: int = 0

        # Identity-match backend — chosen at runtime if the dlib
        # backend is importable; otherwise identity-match runs a
        # no-op backend that always returns a zero vector (so
        # similarity against the enrollment vector is 0, well below
        # any threshold — correct fail-closed behaviour in dev).
        try:
            self._identity_backend: IdentityBackend = FaceRecognitionBackend()
        except ImportError:
            logger.warning(
                "face_recognition not available; identity-match will be "
                "a no-op (similarity always 0.0). Install "
                "face_recognition per the Dockerfile / "
                "pyproject.toml to enable real identity matching."
            )
            self._identity_backend = _AlwaysZeroBackend()

        # Runners — lazily initialised (heavy model loads).
        self._face_landmarker: FaceLandmarkerRunner | None = None
        self._object_detector: ObjectDetectorRunner | None = None
        self._audio_vad: AudioVadRunner | None = None
        self._identity_runner: IdentityMatchRunner | None = None

        if config.policy_snapshot.liveness_check_enabled:
            from proctoring_engine.inference.liveness import (
                LivenessRunner,
                UnifaceBackend,
            )

            try:
                # Can raise EnvironmentError / FileNotFoundError
                # if uniface isn't installed or model isn't found
                liveness_backend = UnifaceBackend()
                self._liveness_runner: LivenessRunner | None = LivenessRunner(
                    liveness_backend
                )
            except Exception as exc:
                logger.warning(
                    "Liveness backend initialization failed; "
                    "anti-spoofing checks are disabled: %s",
                    exc,
                )
                self._liveness_runner = None
        else:
            self._liveness_runner = None

        # Flag decision queue (drained by the persistence layer in
        # turn 9b — this turn only emits to it).
        self.flag_decisions: queue.Queue[FlagDecision] = queue.Queue()

        # Mapping from per-event synthetic UUID to the original
        # BufferedEvent.seq — kept so persistence can correlate
        # TelemetryEvent rows back to the WS-layer arrival order
        # in turn 9b.  Not currently consumed; provided for future
        # use.
        self._event_id_for_seq: dict[int, uuid.UUID] = {}

        # Lifecycle
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._processed_count = 0
        self._error_count = 0

    # -----------------------------------------------------------------
    # Public lifecycle
    # -----------------------------------------------------------------

    def start(self) -> None:
        """Start the background dispatch loop in a daemon thread."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"FrameDispatcher-{self._config.context.exam_session_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "FrameDispatcher started for session %s.",
            self._config.context.exam_session_id,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the dispatch loop and wait for the thread to exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        self._close_runners()

    @property
    def processed_count(self) -> int:
        return self._processed_count

    @property
    def error_count(self) -> int:
        return self._error_count

    # -----------------------------------------------------------------
    # Lazy runner initialisation
    # -----------------------------------------------------------------

    def _ensure_face_landmarker(self) -> FaceLandmarkerRunner:
        if self._face_landmarker is None:
            self._face_landmarker = FaceLandmarkerRunner()
        return self._face_landmarker

    def _ensure_object_detector(self) -> ObjectDetectorRunner:
        if self._object_detector is None:
            # The runner uses the module-level DENYLIST_CLASSES
            # (a frozen set of the four v1-denylisted object names)
            # internally; we do not pass a per-instance denylist here.
            self._object_detector = ObjectDetectorRunner()
        return self._object_detector

    def _ensure_audio_vad(self) -> AudioVadRunner:
        if self._audio_vad is None:
            self._audio_vad = AudioVadRunner(
                noise_floor_dbfs=self._config.policy_snapshot.audio_noise_floor_dbfs,
                speech_ratio_threshold=self._config.policy_snapshot.audio_speech_ratio_threshold,
            )
        return self._audio_vad

    def _ensure_identity_runner(self) -> IdentityMatchRunner:
        if self._identity_runner is None:
            self._identity_runner = IdentityMatchRunner(self._identity_backend)
        return self._identity_runner

    def _close_runners(self) -> None:
        for runner in (
            self._face_landmarker,
            self._object_detector,
            self._audio_vad,
            self._identity_runner,
        ):
            if runner is None:
                continue
            close = getattr(runner, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    logger.exception("Runner close() failed.")

    # -----------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------

    def _run_loop(self) -> None:
        """Poll the buffer, dispatch to runners, push decisions to the queue."""
        while not self._stop_event.is_set():
            try:
                batch = self._buffer.drain()
            except Exception:
                logger.exception("Buffer drain failed.")
                self._error_count += 1
                # Brief sleep to avoid a tight error loop.
                self._stop_event.wait(0.1)
                continue

            for event in batch:
                if self._stop_event.is_set():
                    break
                try:
                    self._dispatch_event(event)
                    self._processed_count += 1
                except Exception:
                    logger.exception(
                        "FrameDispatcher dispatch failed for session %s.",
                        self._config.context.exam_session_id,
                    )
                    self._error_count += 1

            # Sleep if we drained nothing — avoids busy-waiting on
            # an empty buffer.  50 ms keeps tail-latency low while
            # reducing thread overhead.
            if not batch:
                self._stop_event.wait(0.05)

    def _dispatch_event(self, event: BufferedEvent) -> None:
        message = event.message
        # Mint a stable synthetic UUID per buffered seq.  This will
        # become the ``TelemetryEvent.id`` when persistence is wired
        # in turn 9b.  Reusing a single UUID per seq across the
        # session is fine because the dispatcher's buffer drains
        # before reusing seq numbers (monotonic counter in the
        # buffer).
        telemetry_event_id = uuid.uuid4()
        self._event_id_for_seq[event.seq] = telemetry_event_id

        if isinstance(message, TelemetryLight):
            self._dispatch_light(message, telemetry_event_id)
        elif isinstance(message, TelemetryHeavyFrame):
            self._dispatch_heavy_frame(message, telemetry_event_id)
        elif isinstance(message, TelemetryAudioChunk):
            self._dispatch_audio_chunk(message, telemetry_event_id)
        elif isinstance(message, TelemetryBrowserEvent):
            self._dispatch_browser_event(message, telemetry_event_id)
        else:
            # KillSwitchAcknowledge handled elsewhere (delivery service)
            logger.debug(
                "FrameDispatcher ignoring message of type %s.",
                type(message).__name__,
            )

    # -----------------------------------------------------------------
    # Per-message-type dispatchers
    # -----------------------------------------------------------------

    def _dispatch_light(
        self,
        message: TelemetryLight,
        telemetry_event_id: uuid.UUID,
    ) -> None:
        """Client-side face-presence result.  No server-side inference."""
        result = FacePresenceResult(
            modality="face",
            event_type=(
                "second_person"
                if message.payload.face_count >= 2
                else (
                    "no_face"
                    if message.payload.face_count == 0
                    else "one_face"
                )
            ),
            confidence=_confidence_from_float(message.payload.confidence),
            face_count=message.payload.face_count,
        )
        self._emit(
            self.aggregator.process_face_presence(
                result, telemetry_event_id=telemetry_event_id
            )
        )

    def _dispatch_heavy_frame(
        self,
        message: TelemetryHeavyFrame,
        telemetry_event_id: uuid.UUID,
    ) -> None:
        """Decode the JPEG, ask the scheduler, run selected runners."""
        try:
            frame = decode_jpeg_frame(
                message.payload.frame,
                encoding=message.payload.encoding,
            )
        except Exception as exc:
            logger.warning(
                "Heavy frame decode failed for session %s: %s",
                self._config.context.exam_session_id,
                exc,
            )
            self._error_count += 1
            return

        seq = self._heavy_frame_seq
        self._heavy_frame_seq += 1
        decision = self._scheduler.decide_for_frame(seq)

        # Run the face landmarker when either HEAD_POSE_GAZE or
        # IDENTITY_MATCH is scheduled — both need the landmarks.
        landmarker_result = None
        if decision.should_run(InferenceModality.HEAD_POSE_GAZE) or decision.should_run(
            InferenceModality.IDENTITY_MATCH
        ):
            landmarker = self._ensure_face_landmarker()
            try:
                landmarker_result = landmarker.run(frame)
            except Exception:
                logger.exception("FaceLandmarkerRunner.run failed.")
                self._error_count += 1
                landmarker_result = None

        if decision.should_run(InferenceModality.HEAD_POSE_GAZE) and landmarker_result is not None:
            self._emit(
                self.aggregator.process_gaze(
                    landmarker_result,
                    telemetry_event_id=telemetry_event_id,
                    frame_timestamp_ms=_captured_at_ms(message),
                )
            )

        if decision.should_run(InferenceModality.OBJECT_DETECTION):
            detector = self._ensure_object_detector()
            try:
                detections = detector.run(frame)
            except Exception:
                logger.exception("ObjectDetectorRunner.run failed.")
                self._error_count += 1
                detections = []
            for det in detections:
                self._emit(
                    self.aggregator.process_object_detection(
                        det,
                        telemetry_event_id=telemetry_event_id,
                        now=datetime.now(timezone.utc),
                    )
                )

        if decision.should_run(InferenceModality.IDENTITY_MATCH):
            # If the landmarker didn't run this frame, skip identity
            # (no way to crop without landmarks).  We could fall back
            # to running it now, but a single missed identity check
            # is acceptable — the next scheduled frame will retry.
            if landmarker_result is None or self._config.enrollment_embedding is None:
                return

            face_rgb = _extract_face_crop_for_identity(frame, landmarker_result)
            if face_rgb is None:
                # No usable landmarks → no face crop → can't verify
                # identity this frame.  Same skip semantics.
                return

            identity_runner = self._ensure_identity_runner()
            try:
                identity_result = identity_runner.run(
                    face_rgb,
                    self._config.enrollment_embedding,
                    similarity_threshold=self._config.policy_snapshot.identity_similarity_threshold,
                )
            except Exception:
                logger.exception("IdentityMatchRunner.run failed.")
                self._error_count += 1
                return

            self._emit(
                self.aggregator.process_identity_match(
                    identity_result, telemetry_event_id=telemetry_event_id
                )
            )

        if decision.should_run(InferenceModality.LIVENESS) and self._liveness_runner is not None:
            # Liveness needs the uncropped BGR frame and a pixel bbox
            if landmarker_result is None:
                return

            bbox_xyxy = _extract_face_bbox_pixel_coords(frame, landmarker_result)
            if bbox_xyxy is None:
                return

            try:
                liveness_result = self._liveness_runner.run(
                    frame.array,
                    bbox_xyxy,
                    confidence_threshold=self._config.policy_snapshot.liveness_score_threshold,
                )
            except Exception:
                logger.exception("LivenessRunner.run failed.")
                self._error_count += 1
                return

            self._emit(
                self.aggregator.process_liveness(
                    liveness_result, telemetry_event_id=telemetry_event_id
                )
            )

    def _dispatch_audio_chunk(
        self,
        message: TelemetryAudioChunk,
        telemetry_event_id: uuid.UUID,
    ) -> None:
        """Decode the PCM audio, run VAD, push the chunk-level result."""
        try:
            samples = _decode_pcm(message)
        except Exception as exc:
            logger.warning(
                "Audio decode failed for session %s: %s",
                self._config.context.exam_session_id,
                exc,
            )
            self._error_count += 1
            return

        rms_db = _compute_rms_db(samples)

        try:
            frames = split_into_vad_frames(
                samples,
                sample_rate_hz=message.payload.sample_rate_hz,
                duration_ms=message.payload.duration_ms,
            )
        except Exception as exc:
            logger.warning("VAD frame split failed: %s", exc)
            self._error_count += 1
            return

        try:
            vad_runner = self._ensure_audio_vad()
            audio_result = vad_runner.run(frames, rms_db=rms_db)
        except Exception:
            logger.exception("AudioVadRunner.run failed.")
            self._error_count += 1
            return

        # If the chunk was silence, skip dispatching.
        if audio_result.event_type == EVENT_SILENCE:
            return

        self._emit(
            self.aggregator.process_audio_vad(
                audio_result, telemetry_event_id=telemetry_event_id
            )
        )

    def _dispatch_browser_event(
        self,
        message: TelemetryBrowserEvent,
        telemetry_event_id: uuid.UUID,
    ) -> None:
        result = BrowserEventResult(
            modality="browser",
            event_type=message.payload.event_type,
            confidence=ConfidenceInterval(
                lower=1.0, score=1.0, upper=1.0,
            ),
            raw_value={},
            detail=dict(message.payload.detail),
        )
        self._emit(
            self.aggregator.process_browser_event(
                result, telemetry_event_id=telemetry_event_id
            )
        )

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _emit(self, decisions: list[FlagDecision]) -> None:
        """Push all decisions from an aggregator call onto the queue."""
        for d in decisions:
            self.flag_decisions.put(d)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _confidence_from_float(score: float) -> ConfidenceInterval:
    return ConfidenceInterval(lower=score, score=score, upper=score)


def _captured_at_ms(message: Any) -> int:
    """Extract a millisecond timestamp from the envelope's captured_at field."""
    ts = getattr(message, "captured_at", None)
    if isinstance(ts, datetime):
        # Wall-clock captured_at — convert to monotonic epoch ms.
        # Real production code would convert to a per-session-relative
        # timestamp; this is a safe default that always grows.
        return int(ts.timestamp() * 1000)
    return 0


def _decode_pcm(message: TelemetryAudioChunk) -> np.ndarray:
    """Decode a base64 PCM audio chunk to an int16 numpy array.

    Defers to ``preprocessing.audio.decode_pcm_audio_chunk``.
    """
    # Local import keeps the module-level import graph smaller and
    # avoids forcing the audio decoder to load when the dispatcher is
    # imported for non-audio use cases (e.g. CI in a stripped-down
    # environment).
    from proctoring_engine.preprocessing.audio import decode_pcm_audio_chunk

    return decode_pcm_audio_chunk(
        message.payload.audio,
        encoding="pcm16_le",
        sample_rate_hz=message.payload.sample_rate_hz,
    )


def _compute_rms_db(samples: np.ndarray) -> float:
    """Chunk-level RMS in dBFS — mirrors ``preprocessing.audio.compute_rms_db``."""
    if samples.size == 0:
        return float("-inf")
    float_samples = samples.astype(np.float64)
    rms = float(np.sqrt(np.mean(float_samples ** 2)))
    if rms <= 0.0:
        return float("-inf")
    # 32768 is the max amplitude of int16.
    return float(20.0 * np.log10(rms / 32768.0))


def _extract_face_bbox_pixel_coords(
    frame: DecodedFrame,
    head_pose_gaze_result: Any,
) -> list[int] | None:
    """Extract a [x1, y1, x2, y2] bounding box in pixel coordinates."""
    if head_pose_gaze_result is None:
        return None
    landmarks = head_pose_gaze_result.raw_value.get("landmarks")
    if not landmarks:
        return None

    try:
        xs = [p[0] for p in landmarks]
        ys = [p[1] for p in landmarks]
    except (TypeError, IndexError):
        return None

    x_min = max(0.0, min(xs))
    x_max = min(1.0, max(xs))
    y_min = max(0.0, min(ys))
    y_max = min(1.0, max(ys))

    # Convert to pixel coordinates on the BGR frame.
    h, w = frame.array.shape[:2]
    x1 = int(x_min * w)
    x2 = int(x_max * w)
    y1 = int(y_min * h)
    y2 = int(y_max * h)

    # Apply 30% padding — the face contour extends beyond the inner
    # landmark spread.
    pad_x = max(1, int((x2 - x1) * 0.3))
    pad_y = max(1, int((y2 - y1) * 0.3))
    x1 = max(0, x1 - pad_x)
    y1 = max(0, y1 - pad_y)
    x2 = min(w, x2 + pad_x)
    y2 = min(h, y2 + pad_y)

    if x2 <= x1 or y2 <= y1:
        return None

    return [x1, y1, x2, y2]


def _extract_face_crop_for_identity(
    frame: DecodedFrame,
    head_pose_gaze_result: Any,
) -> np.ndarray | None:
    """Extract the primary face crop for identity match.

    Uses the landmarks emitted by ``FaceLandmarkerRunner.run`` (now
    exposed in ``raw_value["landmarks"]`` as of Turn 9a) to approximate
    a bounding box around the face.  Returns the crop as an RGB
    ``np.ndarray`` (the dlib embedder expects RGB), or ``None`` if no
    landmarks are available.
    """
    bbox = _extract_face_bbox_pixel_coords(frame, head_pose_gaze_result)
    if bbox is None:
        return None

    x1, y1, x2, y2 = bbox
    crop_bgr = frame.array[y1:y2, x1:x2]

    # dlib expects RGB; the frame is BGR (preprocessing layer's native
    # format).
    return cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB)


# ---------------------------------------------------------------------------
# Fallback identity backend (dev environments without face_recognition)
# ---------------------------------------------------------------------------


class _AlwaysZeroBackend(IdentityBackend):
    """Fallback identity backend for dev environments without face_recognition.

    ``embed`` returns a zero vector so ``compute_cosine_similarity``
    against any enrollment vector is always 0.0 — well below any
    plausible threshold.  This means identity-match in dev
    environments fires the CRITICAL path on the first sampled window,
    which is the correct fail-closed behaviour (no real
    face_recognition = no identity verification).
    """

    def embed(self, face_rgb: np.ndarray) -> list[float]:
        return [0.0] * 128

    @property
    def model_name(self) -> str:
        return "always-zero-fallback"

    @property
    def embedding_dim(self) -> int:
        return 128
