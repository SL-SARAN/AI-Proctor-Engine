"""Audio preprocessing for the VAD inference path.

The ingestion layer accepts audio chunks at exactly the sample rates
``webrtcvad`` supports (8000 / 16000 / 32000 / 48000 Hz) and at
frame durations of 10 / 20 / 30 ms — see
``docs/02-ingestion-layer-design.md`` §3 (``audio_chunk`` payload)
and ``docs/03-preprocessing-layer-design.md`` §4.

This module is the *server-side* counterpart to that contract:

- **Decode** the base64-encoded chunk into raw int16 PCM samples.
- **Resample** chunks that arrived at a non-canonical rate to the
  closest VAD-compatible rate (this is a server-side safety net for
  clients that misbehave — the design says the client is responsible
  for resampling, but a *fail-soft* server doesn't reject good
  detections from a slightly out-of-spec client).
- **Split** the chunk into the exact 10 / 20 / 30 ms frames the VAD
  library requires.
- **Compute RMS** over the chunk for the ambient-noise heuristic
  documented in ``docs/04-inference-modules-design.md`` §5.
"""

from __future__ import annotations

import base64
import enum
import math
from dataclasses import dataclass
from typing import Final, Iterable

import numpy as np


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Sample rates ``webrtcvad`` accepts (Hz).
VAD_SAMPLE_RATES: Final[frozenset[int]] = frozenset({8000, 16000, 32000, 48000})

#: Frame durations ``webrtcvad`` accepts (ms).
VAD_FRAME_DURATIONS: Final[frozenset[int]] = frozenset({10, 20, 30})


# ---------------------------------------------------------------------------
# Error surface
# ---------------------------------------------------------------------------

class AudioDecodeError(Exception):
    """Raised when an audio chunk cannot be decoded or normalised."""


class DecoderUnknownEncoding(Exception):
    """Raised for audio chunks that declare an unrecognised encoding.

    This is a distinct type so the server can map it to a separate
    close-code / log line, rather than a generic decode error.
    """


# ---------------------------------------------------------------------------
# Encoding enum + base decoder
# ---------------------------------------------------------------------------

class _AudioEncoding(str, enum.Enum):
    PCM_16_LE = "pcm16_le"
    PCM_16_BE = "pcm16_be"
    OPUS = "opus"
    UNKNOWN = "unknown"


def _coerce_encoding(value: str | None) -> _AudioEncoding:
    """Map a wire-format encoding name to the enum, tolerant of
    case and a couple of common aliases.
    """

    if value is None:
        return _AudioEncoding.UNKNOWN
    norm = value.strip().lower().replace("-", "_")
    if norm in ("pcm16_le", "pcm_le", "s16_le", "signed16_le"):
        return _AudioEncoding.PCM_16_LE
    if norm in ("pcm16_be", "pcm_be", "s16_be", "signed16_be"):
        return _AudioEncoding.PCM_16_BE
    if norm in ("opus", "ogg_opus"):
        return _AudioEncoding.OPUS
    return _AudioEncoding.UNKNOWN


# Concrete decoder entry-points the rest of the module dispatches to.
# Each returns an int16 numpy array of the chunk's samples.  They
# are *not* part of the public surface — callers go through
# :func:`decode_pcm_audio_chunk`.


def DecoderPcm16Le(encoded: str) -> np.ndarray:  # noqa: N802 — public-facing name
    """Decode a base64 little-endian int16 PCM chunk."""

    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise AudioDecodeError(
            f"chunk payload is not valid base64: {exc}"
        ) from exc

    if len(raw) % 2 != 0:
        raise AudioDecodeError(
            f"PCM-16-LE chunk has an odd byte count ({len(raw)}); "
            f"each sample is 2 bytes."
        )
    return np.frombuffer(raw, dtype="<i2")


def DecoderPcm16Be(encoded: str) -> np.ndarray:  # noqa: N802 — public-facing name
    """Decode a base64 big-endian int16 PCM chunk."""

    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise AudioDecodeError(
            f"chunk payload is not valid base64: {exc}"
        ) from exc

    if len(raw) % 2 != 0:
        raise AudioDecodeError(
            f"PCM-16-BE chunk has an odd byte count ({len(raw)}); "
            f"each sample is 2 bytes."
        )
    return np.frombuffer(raw, dtype=">i2")


_DECODERS = {
    _AudioEncoding.PCM_16_LE: DecoderPcm16Le,
    _AudioEncoding.PCM_16_BE: DecoderPcm16Be,
}


# ---------------------------------------------------------------------------
# Audio frame container
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AudioFrame:
    """A single ``webrtcvad``-compatible frame.

    ``samples`` is an ``int16`` numpy array of the canonical length
    ``sample_rate_hz * duration_ms / 1000`` — e.g. 160 samples for a
    10 ms frame at 16 kHz.
    """

    samples: np.ndarray
    sample_rate_hz: int
    duration_ms: int
    captured_at_ms: int  # Offset from the start of the chunk.


# ---------------------------------------------------------------------------
# Decode entry-point
# ---------------------------------------------------------------------------

def decode_pcm_audio_chunk(
    encoded: str,
    *,
    encoding: str = "pcm16_le",
    sample_rate_hz: int | None = None,
) -> np.ndarray:
    """Decode a base64 audio chunk to an int16 numpy array.

    Parameters
    ----------
    encoded:
        Base64-encoded audio bytes.
    encoding:
        Wire-format encoding.  Currently supported:
        ``"pcm16_le"`` (default, little-endian int16) and
        ``"pcm16_be"`` (big-endian int16).  ``"opus"`` is recognised
        but rejected with :class:`DecoderUnknownEncoding` for v1
        (Opus decode would require an extra native dep that is not
        in scope).
    sample_rate_hz:
        The sample rate the client *claims* it is sending at.  This
        is *not* verified at decode time — it is returned alongside
        the samples for downstream processing.  If ``None`` or not
        one of the four VAD rates, callers should pass the chunk to
        :func:`resample_to_vad_rate` first.
    """

    enc = _coerce_encoding(encoding)
    if enc is _AudioEncoding.OPUS:
        raise DecoderUnknownEncoding(
            "Opus decoding is not supported in v1; clients must send "
            "PCM-16 (LE or BE)."
        )
    if enc is _AudioEncoding.UNKNOWN:
        raise DecoderUnknownEncoding(
            f"Unknown audio encoding '{encoding}'; expected one of "
            "['pcm16_le', 'pcm16_be']."
        )

    samples = _DECODERS[enc](encoded)
    if sample_rate_hz is not None and sample_rate_hz not in VAD_SAMPLE_RATES:
        # Surface, don't silently resample.  Callers can opt into
        # ``resample_to_vad_rate`` explicitly.
        raise AudioDecodeError(
            f"sample_rate_hz {sample_rate_hz} is not one of "
            f"{sorted(VAD_SAMPLE_RATES)}; client must resample or "
            f"caller must invoke resample_to_vad_rate()."
        )
    return samples


# ---------------------------------------------------------------------------
# Resampling (server-side safety net for misbehaving clients)
# ---------------------------------------------------------------------------

def resample_to_vad_rate(
    samples: np.ndarray,
    *,
    from_rate: int,
    to_rate: int,
) -> np.ndarray:
    """Resample a PCM int16 array from one VAD rate to another.

    The four VAD rates are exact integer multiples of each other
    (8000 * 2 = 16000, * 2 = 32000, * 1.5 = 48000), so a simple
    linear resampler with the right ratio is exact for power-of-two
    rate pairs and adequate for the 48 kHz case.

    The resampler is **not** intended as a high-quality DSP solution;
    it is a server-side safety net for clients that arrive at the
    wrong rate despite the spec telling them to resample on the
    client side.  The client is the source of truth for rate.
    """

    if from_rate not in VAD_SAMPLE_RATES:
        raise AudioDecodeError(
            f"resample: from_rate {from_rate} is not a VAD-supported rate."
        )
    if to_rate not in VAD_SAMPLE_RATES:
        raise AudioDecodeError(
            f"resample: to_rate {to_rate} is not a VAD-supported rate."
        )
    if from_rate == to_rate:
        return np.array(samples, copy=True)

    ratio = to_rate / from_rate
    # Linear interpolation; for 8/16/32 kHz it's exact (ratio is
    # 1, 2, 0.5), for 48 kHz from 32 kHz it's 1.5 which gives a
    # reasonable (if not studio-quality) approximation.
    src_len = len(samples)
    dst_len = int(round(src_len * ratio))
    if dst_len == 0:
        return np.array([], dtype=samples.dtype)

    # Avoid division by zero when the input is a single sample.
    if src_len == 1:
        return np.full(dst_len, samples[0], dtype=samples.dtype)

    src_x = np.arange(src_len, dtype=np.float64)
    dst_x = np.linspace(0.0, src_len - 1, num=dst_len, dtype=np.float64)
    resampled = np.interp(dst_x, src_x, samples.astype(np.float64))
    return np.clip(resampled, -32768, 32767).astype(np.int16)


# ---------------------------------------------------------------------------
# Frame splitting
# ---------------------------------------------------------------------------

def split_into_vad_frames(
    samples: np.ndarray,
    *,
    sample_rate_hz: int,
    duration_ms: int,
    captured_at_offset_ms: int = 0,
) -> list[AudioFrame]:
    """Split a PCM chunk into a list of exact VAD frame durations.

    The last frame is **padded with silence** if the chunk's
    duration is not an exact multiple of the frame length — this
    matches ``webrtcvad``'s contract (which expects fixed-length
    inputs) without losing any data.

    Returns a list of :class:`AudioFrame` objects, each carrying the
    samples, the rate, the duration, and the chunk-relative
    ``captured_at_ms`` offset.
    """

    if sample_rate_hz not in VAD_SAMPLE_RATES:
        raise AudioDecodeError(
            f"split: sample_rate_hz {sample_rate_hz} is not VAD-supported."
        )
    if duration_ms not in VAD_FRAME_DURATIONS:
        raise AudioDecodeError(
            f"split: duration_ms {duration_ms} is not VAD-supported; "
            f"expected one of {sorted(VAD_FRAME_DURATIONS)}."
        )

    samples_per_frame = sample_rate_hz * duration_ms // 1000
    if samples_per_frame == 0:
        raise AudioDecodeError(
            f"split: rate/duration combination {sample_rate_hz} Hz / "
            f"{duration_ms} ms produced a 0-sample frame."
        )

    frames: list[AudioFrame] = []
    if len(samples) == 0:
        return frames

    n_full = len(samples) // samples_per_frame
    tail = len(samples) % samples_per_frame

    for i in range(n_full):
        start = i * samples_per_frame
        chunk = samples[start : start + samples_per_frame]
        frames.append(
            AudioFrame(
                samples=chunk,
                sample_rate_hz=sample_rate_hz,
                duration_ms=duration_ms,
                captured_at_ms=captured_at_offset_ms + i * duration_ms,
            )
        )

    if tail:
        start = n_full * samples_per_frame
        chunk = np.zeros(samples_per_frame, dtype=samples.dtype)
        chunk[:tail] = samples[start:]
        frames.append(
            AudioFrame(
                samples=chunk,
                sample_rate_hz=sample_rate_hz,
                duration_ms=duration_ms,
                captured_at_ms=captured_at_offset_ms + n_full * duration_ms,
            )
        )

    return frames


# ---------------------------------------------------------------------------
# Decibel / RMS calculation
# ---------------------------------------------------------------------------

def compute_rms_db(samples: np.ndarray) -> float:
    """Compute the RMS amplitude of an int16 PCM array, in dBFS.

    ``dBFS`` is "decibels relative to full scale" — ``0 dBFS`` is the
    loudest possible level (all samples at ±32767), and negative
    values are quieter.  A value of ``-60 dBFS`` or below is
    effectively silence on most recordings.

    The inference layer uses this for the ambient-noise heuristic
    (``docs/04-inference-modules-design.md`` §5): a sustained RMS
    above the noise floor means a speaker is present even when VAD
    is mid-silence.
    """

    if len(samples) == 0:
        return float("-inf")

    # Use float64 for numerical stability across large sample blocks.
    as_float = samples.astype(np.float64)
    rms = float(np.sqrt(np.mean(as_float * as_float)))
    if rms <= 0.0:
        return float("-inf")

    # Full-scale int16 amplitude is 32768 (one above the max value).
    return 20.0 * math.log10(rms / 32768.0)


# ---------------------------------------------------------------------------
# Convenience: end-to-end pipeline
# ---------------------------------------------------------------------------

def preprocess_audio_chunk(
    encoded: str,
    *,
    encoding: str = "pcm16_le",
    sample_rate_hz: int,
    chunk_duration_ms: int,
    frame_duration_ms: int = 20,
) -> tuple[list[AudioFrame], float]:
    """Run the full audio-preprocessing pipeline on one chunk.

    Returns a tuple of ``(frames, rms_db)``:

    - ``frames`` — the chunk split into VAD-compatible frames,
      ready for ``webrtcvad``.
    - ``rms_db`` — the chunk's RMS level in dBFS, for the
      ambient-noise heuristic in the inference layer.

    If the chunk's rate isn't in the VAD rate set, it is silently
    resampled to the nearest VAD rate — this is the documented
    server-side safety net.
    """

    if frame_duration_ms not in VAD_FRAME_DURATIONS:
        raise AudioDecodeError(
            f"frame_duration_ms {frame_duration_ms} is not VAD-supported."
        )

    samples = decode_pcm_audio_chunk(
        encoded,
        encoding=encoding,
        sample_rate_hz=sample_rate_hz,
    )
    if sample_rate_hz not in VAD_SAMPLE_RATES:
        target = _nearest_vad_rate(sample_rate_hz)
        samples = resample_to_vad_rate(
            samples, from_rate=sample_rate_hz, to_rate=target
        )
        rate_for_split = target
    else:
        rate_for_split = sample_rate_hz

    rms_db = compute_rms_db(samples)
    frames = split_into_vad_frames(
        samples,
        sample_rate_hz=rate_for_split,
        duration_ms=frame_duration_ms,
    )
    return frames, rms_db


def _nearest_vad_rate(rate: int) -> int:
    """Return the VAD-supported rate closest to ``rate``.

    Used as the resample target when a chunk arrives at a non-VAD
    rate.  Ties break to the higher rate (more samples = finer
    resolution for VAD).
    """

    candidates: Iterable[int] = sorted(VAD_SAMPLE_RATES)
    return min(
        candidates,
        key=lambda r: (abs(r - rate), -r),
    )
