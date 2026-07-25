"""Preprocessing layer package.

Sits between ingestion (raw envelopes arriving on the WebSocket) and
the inference modules (which expect clean, model-ready input). The
layer is intentionally *parse and forward* at its boundary — see
``docs/03-preprocessing-layer-design.md``.

Public subsets:

- :mod:`proctoring_engine.preprocessing.frames` — JPEG decode + per-model
  normalisation (BGR->RGB for MediaPipe, raw for YOLOv8, cropped-face
  pass-through for identity match).
- :mod:`proctoring_engine.preprocessing.audio` — base64 -> PCM decode,
  VAD-rate enforcement, frame splitting for ``webrtcvad``, RMS decibel
  calculation.
- :mod:`proctoring_engine.preprocessing.scheduler` — modality-keyed
  cadence decisions for which inference modules to invoke on which
  ingested frame / chunk.
- :mod:`proctoring_engine.preprocessing.rolling_buffer` — the *server-
  side* receiver contract for flushed client rolling-buffer contents.
  The browser-side buffer itself lives in the (unbuilt) client layer.
"""

from proctoring_engine.preprocessing.frames import (
    ACCEPTED_ENCODINGS,
    DecodedFrame,
    FrameDecodeError,
    decode_jpeg_frame,
    normalize_for_mediapipe,
    normalize_for_yolov8,
)
from proctoring_engine.preprocessing.audio import (
    AudioDecodeError,
    AudioFrame,
    DecoderPcm16Be,
    DecoderPcm16Le,
    DecoderUnknownEncoding,
    VAD_FRAME_DURATIONS,
    VAD_SAMPLE_RATES,
    compute_rms_db,
    decode_pcm_audio_chunk,
    preprocess_audio_chunk,
    resample_to_vad_rate,
    split_into_vad_frames,
)
from proctoring_engine.preprocessing.scheduler import (
    InferenceDecision,
    InferenceModality,
    ModalityDecision,
    ModalityScheduler,
    ScheduleDecision,
)
from proctoring_engine.preprocessing.rolling_buffer import (
    BufferFlushError,
    InMemoryRollingBuffer,
    NullRollingBuffer,
    RollingBuffer,
    RollingBufferConfig,
    RollingBufferEntry,
)

__all__ = [
    # frames
    "ACCEPTED_ENCODINGS",
    "DecodedFrame",
    "FrameDecodeError",
    "decode_jpeg_frame",
    "normalize_for_mediapipe",
    "normalize_for_yolov8",
    # audio
    "AudioDecodeError",
    "AudioFrame",
    "DecoderPcm16Be",
    "DecoderPcm16Le",
    "DecoderUnknownEncoding",
    "VAD_FRAME_DURATIONS",
    "VAD_SAMPLE_RATES",
    "compute_rms_db",
    "decode_pcm_audio_chunk",
    "preprocess_audio_chunk",
    "resample_to_vad_rate",
    "split_into_vad_frames",
    # scheduler
    "InferenceDecision",
    "InferenceModality",
    "ModalityDecision",
    "ModalityScheduler",
    "ScheduleDecision",
    # rolling buffer
    "BufferFlushError",
    "InMemoryRollingBuffer",
    "NullRollingBuffer",
    "RollingBuffer",
    "RollingBufferConfig",
    "RollingBufferEntry",
]
