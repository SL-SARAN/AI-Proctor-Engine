"""Audio voice-activity detection inference module.

Wraps ``webrtcvad-wheels`` (``import webrtcvad``, MIT-licensed fork
of the base ``webrtcvad`` with prebuilt wheels for
Windows/macOS/Linux) — the v1 VAD library per the 2026-07-24 spec
amendment.

This module is the server-side inference step that sits between the
preprocessing layer's audio decode/split pipeline
(``proctoring_engine.preprocessing.audio``) and the fusion engine.

**What this module does:**

1. Runs ``webrtcvad.Vad.is_speech()`` on each preprocessed
   :class:`AudioFrame` and counts speech-vs-silence frames.
2. Combines the speech ratio with the chunk-level RMS dBFS (computed
   by the preprocessing layer) to produce a classified event:

   - ``"silence"`` — VAD says no speech AND RMS below noise floor.
   - ``"speech_detected"`` — VAD says speech.
   - ``"elevated_rms"`` — RMS above noise floor but VAD says silence
     (the ambient-noise heuristic documented in
     ``docs/04-inference-modules-design.md`` §5).

3. Emits an :class:`AudioVadResult` with the speech ratio, RMS,
   and per-frame counts.

**Multi-speaker detection is explicitly NOT in v1** — full
diarization (``pyannote.audio``) requires Hugging Face–hosted models
with license gating, deferred per the spec.

Input:  ``list[AudioFrame]`` + ``rms_db: float`` from the
        preprocessing pipeline.
Output: an :class:`AudioVadResult`.
"""

from __future__ import annotations

import logging
from typing import Final

import numpy as np

from proctoring_engine.inference._types import (
    AudioVadResult,
    ConfidenceInterval,
)
from proctoring_engine.preprocessing.audio import AudioFrame


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MODALITY: Final[str] = "audio"

EVENT_SILENCE: Final[str] = "silence"
EVENT_SPEECH_DETECTED: Final[str] = "speech_detected"
EVENT_ELEVATED_RMS: Final[str] = "elevated_rms"

# Default noise-floor threshold in dBFS.  A sustained RMS above this
# in the absence of VAD-detected speech triggers ``elevated_rms``.
# -30 dBFS is approximately the level of quiet conversation at arm's
# length.  This is a constructor argument, not hardcoded.
_DEFAULT_NOISE_FLOOR_DBFS: Final[float] = -30.0

# Default minimum speech ratio for the ``speech_detected`` event.
# A single VAD-positive frame out of an entire chunk was triggering
# speech detection — the 04-inference-modules-design.md §5 spec
# explicitly framed speech detection as "consistent speech
# activity... not every individual VAD frame", so we require the
# speech ratio to clear this minimum before classifying the chunk
# as ``speech_detected``.  ``0.3`` is a calibration starting point
# (require at least 30% of the chunk's frames to be classified as
# speech); it should be tuned against real session audio the same
# way the gaze thresholds are.
_DEFAULT_SPEECH_RATIO_THRESHOLD: Final[float] = 0.3

# Valid aggressiveness modes for ``webrtcvad.Vad``.
_VALID_AGGRESSIVENESS: Final[frozenset[int]] = frozenset({0, 1, 2, 3})


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class AudioVadRunner:
    """Per-chunk audio voice-activity classifier.

    Parameters
    ----------
    aggressiveness:
        ``webrtcvad`` aggressiveness mode (0–3).  Higher is more
        aggressive at filtering non-speech.  Default 2 (balanced).
    noise_floor_dbfs:
        RMS threshold (dBFS) above which the ``elevated_rms`` event
        fires when VAD says silence.  Default -30.0.
    speech_ratio_threshold:
        Minimum fraction of the chunk's frames that must be classified
        as speech for the chunk to be labeled ``speech_detected``.
        Default 0.3 — a single noisy VAD-positive frame is no longer
        sufficient to trigger a speech event.  Calibration starting
        point; tune against real session audio.

    Raises
    ------
    ValueError
        If ``aggressiveness`` is not in ``{0, 1, 2, 3}`` or if
        ``speech_ratio_threshold`` is not in ``[0, 1]``.
    """

    def __init__(
        self,
        *,
        aggressiveness: int = 2,
        noise_floor_dbfs: float = _DEFAULT_NOISE_FLOOR_DBFS,
        speech_ratio_threshold: float = _DEFAULT_SPEECH_RATIO_THRESHOLD,
    ) -> None:
        if aggressiveness not in _VALID_AGGRESSIVENESS:
            raise ValueError(
                f"aggressiveness must be one of {sorted(_VALID_AGGRESSIVENESS)}; "
                f"got {aggressiveness}."
            )
        if not (0.0 <= speech_ratio_threshold <= 1.0):
            raise ValueError(
                f"speech_ratio_threshold must be in [0, 1]; "
                f"got {speech_ratio_threshold}."
            )
        self._noise_floor = noise_floor_dbfs
        self._speech_ratio_threshold = speech_ratio_threshold

        import webrtcvad
        self._vad = webrtcvad.Vad(aggressiveness)

    def run(
        self,
        frames: list[AudioFrame],
        rms_db: float,
    ) -> AudioVadResult:
        """Classify a chunk of audio frames for voice activity.

        Parameters
        ----------
        frames:
            VAD-compatible frames from the preprocessing layer's
            ``split_into_vad_frames`` function.
        rms_db:
            Chunk-level RMS in dBFS from the preprocessing layer's
            ``compute_rms_db`` function.

        Returns
        -------
        AudioVadResult
            ``event_type`` is one of ``"silence"``,
            ``"speech_detected"``, or ``"elevated_rms"``.
        """

        total = len(frames)
        if total == 0:
            return AudioVadResult(
                modality=_MODALITY,
                event_type=EVENT_SILENCE,
                confidence=ConfidenceInterval(lower=0.0, score=0.0, upper=0.0),
                raw_value={"speech_frames": 0, "total_frames": 0, "rms_db": rms_db},
                speech_ratio=0.0,
                rms_db=rms_db,
                speech_frames=0,
                total_frames=0,
            )

        speech_count = 0
        for af in frames:
            # ``webrtcvad.Vad.is_speech`` expects raw ``bytes`` of the
            # PCM samples (int16, native byte order on the wire; the
            # preprocessing layer normalises to little-endian int16).
            pcm_bytes = af.samples.astype("<i2").tobytes()
            try:
                if self._vad.is_speech(pcm_bytes, af.sample_rate_hz):
                    speech_count += 1
            except Exception:
                # A malformed frame should not crash the whole chunk.
                logger.warning(
                    "webrtcvad.is_speech raised for frame at %d ms; "
                    "treating as non-speech.",
                    af.captured_at_ms,
                    exc_info=True,
                )

        speech_ratio = speech_count / total

        # Classification logic.
        if speech_ratio >= self._speech_ratio_threshold:
            event_type = EVENT_SPEECH_DETECTED
        elif rms_db > self._noise_floor:
            event_type = EVENT_ELEVATED_RMS
        else:
            event_type = EVENT_SILENCE

        confidence = ConfidenceInterval(
            lower=speech_ratio,
            score=speech_ratio,
            upper=speech_ratio,
        )

        return AudioVadResult(
            modality=_MODALITY,
            event_type=event_type,
            confidence=confidence,
            raw_value={
                "speech_frames": speech_count,
                "total_frames": total,
                "rms_db": rms_db,
            },
            speech_ratio=speech_ratio,
            rms_db=rms_db,
            speech_frames=speech_count,
            total_frames=total,
        )
