"""Unit tests for proctoring_engine.preprocessing (frames, audio, scheduler, rolling buffer).

These tests exercise every public function and class in the preprocessing
layer against deterministic payloads.  They run on any platform with
``opencv-python-headless``, ``numpy``, and ``Pillow`` installed — no
model weights, no GPU, no external service.

Test fixtures use small (4×4) images and short (160-sample) audio
chunks to keep payloads small and deterministic.
"""

from __future__ import annotations

import base64
import math
import struct
from datetime import datetime, timezone

import cv2
import numpy as np
import pytest

from proctoring_engine.preprocessing.frames import (
    ACCEPTED_ENCODINGS,
    DecodedFrame,
    FrameDecodeError,
    decode_jpeg_frame,
    normalize_for_mediapipe,
    normalize_for_yolov8,
    _is_opencv_array,
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
    _nearest_vad_rate,
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
    _approx_decoded_size,
)


# ======================================================================
# Helpers / fixtures
# ======================================================================

def _make_4x4_bgr_jpeg() -> str:
    """Return a base64-encoded JPEG of a 4×4 BGR image.

    B=50, G=100, R=200 in every pixel.  At 4×4, JPEG encodes and
    decodes losslessly (too small for DCT to distort).
    """
    img = np.zeros((4, 4, 3), dtype=np.uint8)
    img[:, :, 2] = 200  # R
    img[:, :, 1] = 100  # G
    img[:, :, 0] = 50   # B
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return base64.b64encode(buf.tobytes()).decode()


def _make_4x4_bgr_png() -> str:
    """Return a base64-encoded PNG of the same 4×4 BGR image."""
    img = np.zeros((4, 4, 3), dtype=np.uint8)
    img[:, :, 2] = 200
    img[:, :, 1] = 100
    img[:, :, 0] = 50
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return base64.b64encode(buf.tobytes()).decode()


def _make_4x4_bgra_png() -> str:
    """Return a base64-encoded PNG with alpha (BGRA, 4-channel)."""
    img = np.zeros((4, 4, 4), dtype=np.uint8)
    img[:, :, 2] = 200  # R
    img[:, :, 1] = 100  # G
    img[:, :, 0] = 50   # B
    img[:, :, 3] = 255  # A (fully opaque)
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return base64.b64encode(buf.tobytes()).decode()


def _make_pcm16_le_b64(samples: np.ndarray) -> str:
    """Encode int16 samples as base64 little-endian PCM."""
    return base64.b64encode(samples.astype("<i2").tobytes()).decode()


def _make_pcm16_be_b64(samples: np.ndarray) -> str:
    """Encode int16 samples as base64 big-endian PCM."""
    return base64.b64encode(samples.astype(">i2").tobytes()).decode()


def _sine_samples(n_samples: int, freq_hz: int = 1000,
                  sample_rate: int = 16000, amplitude: int = 16000) -> np.ndarray:
    """Generate a short sine wave as int16 samples."""
    t = np.arange(n_samples, dtype=np.float64) / sample_rate
    return (np.sin(2 * np.pi * freq_hz * t) * amplitude).astype(np.int16)


# ======================================================================
# Section 1 — frames.py
# ======================================================================


class TestAcceptedEncodings:
    """Test the ACCEPTED_ENCODINGS constant."""

    def test_contains_jpeg_png_webp(self) -> None:
        assert ACCEPTED_ENCODINGS == frozenset({"jpeg", "png", "webp"})

    def test_is_frozen(self) -> None:
        with pytest.raises(AttributeError):
            ACCEPTED_ENCODINGS.add("bmp")  # type: ignore[attr-defined]


class TestDecodeJpegFrame:
    """Test decode_jpeg_frame (covers JPEG, PNG, WebP paths)."""

    def test_decode_valid_jpeg(self) -> None:
        b64 = _make_4x4_bgr_jpeg()
        frame = decode_jpeg_frame(b64, encoding="jpeg")
        assert isinstance(frame, DecodedFrame)
        assert frame.width == 4
        assert frame.height == 4
        assert frame.channels == 3
        assert frame.encoding == "jpeg"
        assert frame.array.dtype == np.uint8
        assert frame.array.shape == (4, 4, 3)

    def test_decode_valid_png(self) -> None:
        b64 = _make_4x4_bgr_png()
        frame = decode_jpeg_frame(b64, encoding="png")
        assert frame.encoding == "png"
        assert frame.width == 4
        assert frame.height == 4
        # PNG is lossless — exact pixel values.
        assert frame.array[0, 0].tolist() == [50, 100, 200]

    def test_pixel_values_bgr_order(self) -> None:
        """After decode, pixels are in BGR order (B=50, G=100, R=200)."""
        b64 = _make_4x4_bgr_png()
        frame = decode_jpeg_frame(b64, encoding="png")
        px = frame.array[0, 0].tolist()
        assert px == [50, 100, 200], f"Expected BGR [50,100,200], got {px}"

    def test_empty_payload_raises(self) -> None:
        with pytest.raises(FrameDecodeError, match="empty"):
            decode_jpeg_frame("", encoding="jpeg")

    def test_whitespace_only_payload_raises(self) -> None:
        with pytest.raises(FrameDecodeError, match="empty"):
            decode_jpeg_frame("   \n\t", encoding="jpeg")

    def test_invalid_base64_raises(self) -> None:
        with pytest.raises(FrameDecodeError, match="not valid base64"):
            decode_jpeg_frame("!!!not-base64!!!", encoding="jpeg")

    def test_unsupported_encoding_raises(self) -> None:
        with pytest.raises(FrameDecodeError, match="not supported"):
            decode_jpeg_frame(_make_4x4_bgr_jpeg(), encoding="bmp")

    def test_truncated_payload_raises(self) -> None:
        """A valid base64 string that decodes to truncated bytes."""
        valid = _make_4x4_bgr_jpeg()
        truncated = valid[:20]  # Too short to be a valid image
        with pytest.raises(FrameDecodeError, match="imdecode returned None"):
            decode_jpeg_frame(truncated, encoding="jpeg")

    def test_decoded_frame_is_frozen(self) -> None:
        b64 = _make_4x4_bgr_jpeg()
        frame = decode_jpeg_frame(b64, encoding="jpeg")
        with pytest.raises(AttributeError):
            frame.width = 99  # type: ignore[misc]


class TestNormalizeForMediapipe:
    """Test the BGR→RGB swap for MediaPipe."""

    def test_bgr_to_rgb_swap(self) -> None:
        """The B and R channels must swap: BGR[50,100,200] → RGB[200,100,50]."""
        b64 = _make_4x4_bgr_png()
        frame = decode_jpeg_frame(b64, encoding="png")
        rgb_frame = normalize_for_mediapipe(frame)
        px = rgb_frame.array[0, 0].tolist()
        assert px == [200, 100, 50], f"Expected RGB [200,100,50], got {px}"

    def test_encoding_changes_to_rgb(self) -> None:
        frame = decode_jpeg_frame(_make_4x4_bgr_png(), encoding="png")
        rgb_frame = normalize_for_mediapipe(frame)
        assert rgb_frame.encoding == "rgb"

    def test_channels_is_3(self) -> None:
        frame = decode_jpeg_frame(_make_4x4_bgr_png(), encoding="png")
        rgb_frame = normalize_for_mediapipe(frame)
        assert rgb_frame.channels == 3

    def test_bgra_to_rgb_drops_alpha(self) -> None:
        """4-channel BGRA input → strip alpha → swap BGR→RGB."""
        b64 = _make_4x4_bgra_png()
        # Decode with IMREAD_UNCHANGED to get 4 channels
        raw = base64.b64decode(b64)
        arr = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        assert arr.shape[2] == 4
        frame = DecodedFrame(
            array=arr,
            width=arr.shape[1],
            height=arr.shape[0],
            channels=4,
            encoding="png",
        )
        rgb_frame = normalize_for_mediapipe(frame)
        assert rgb_frame.channels == 3
        px = rgb_frame.array[0, 0].tolist()
        assert px == [200, 100, 50]

    def test_dimensions_preserved(self) -> None:
        frame = decode_jpeg_frame(_make_4x4_bgr_png(), encoding="png")
        rgb_frame = normalize_for_mediapipe(frame)
        assert rgb_frame.width == frame.width
        assert rgb_frame.height == frame.height


class TestNormalizeForYolov8:
    """Test the YOLOv8 pass-through normalisation."""

    def test_returns_same_frame(self) -> None:
        frame = decode_jpeg_frame(_make_4x4_bgr_png(), encoding="png")
        out = normalize_for_yolov8(frame)
        assert out is frame

    def test_encoding_unchanged(self) -> None:
        frame = decode_jpeg_frame(_make_4x4_bgr_png(), encoding="png")
        out = normalize_for_yolov8(frame)
        assert out.encoding == "png"


class TestIsOpencvArray:
    """Test the _is_opencv_array utility."""

    def test_valid_3_channel(self) -> None:
        arr = np.zeros((10, 10, 3), dtype=np.uint8)
        assert _is_opencv_array(arr) is True

    def test_valid_4_channel(self) -> None:
        arr = np.zeros((10, 10, 4), dtype=np.uint8)
        assert _is_opencv_array(arr) is True

    def test_wrong_dtype(self) -> None:
        arr = np.zeros((10, 10, 3), dtype=np.float32)
        assert _is_opencv_array(arr) is False

    def test_wrong_ndim(self) -> None:
        arr = np.zeros((10, 10), dtype=np.uint8)
        assert _is_opencv_array(arr) is False

    def test_wrong_channels(self) -> None:
        arr = np.zeros((10, 10, 2), dtype=np.uint8)
        assert _is_opencv_array(arr) is False

    def test_not_an_array(self) -> None:
        assert _is_opencv_array("hello") is False


# ======================================================================
# Section 2 — audio.py
# ======================================================================


class TestVadConstants:
    """Verify the VAD constant sets from the webrtcvad spec."""

    def test_sample_rates(self) -> None:
        assert VAD_SAMPLE_RATES == frozenset({8000, 16000, 32000, 48000})

    def test_frame_durations(self) -> None:
        assert VAD_FRAME_DURATIONS == frozenset({10, 20, 30})


class TestDecoderPcm16Le:
    """Test the little-endian PCM decoder."""

    def test_decode_valid(self) -> None:
        samples = _sine_samples(160)
        b64 = _make_pcm16_le_b64(samples)
        result = DecoderPcm16Le(b64)
        np.testing.assert_array_equal(result, samples)

    def test_empty_payload(self) -> None:
        b64 = base64.b64encode(b"").decode()
        result = DecoderPcm16Le(b64)
        assert len(result) == 0

    def test_odd_byte_count_raises(self) -> None:
        b64 = base64.b64encode(b"\x00\x01\x02").decode()  # 3 bytes
        with pytest.raises(AudioDecodeError, match="odd byte count"):
            DecoderPcm16Le(b64)

    def test_invalid_base64_raises(self) -> None:
        with pytest.raises(AudioDecodeError, match="not valid base64"):
            DecoderPcm16Le("!!!bad!!!")


class TestDecoderPcm16Be:
    """Test the big-endian PCM decoder."""

    def test_decode_valid(self) -> None:
        samples = _sine_samples(80)
        b64 = _make_pcm16_be_b64(samples)
        result = DecoderPcm16Be(b64)
        # Big-endian round-trip: encode BE, decode BE → same values
        np.testing.assert_array_equal(result, samples)

    def test_odd_byte_count_raises(self) -> None:
        b64 = base64.b64encode(b"\x00\x01\x02").decode()
        with pytest.raises(AudioDecodeError, match="odd byte count"):
            DecoderPcm16Be(b64)


class TestDecodePcmAudioChunk:
    """Test the decode_pcm_audio_chunk entry-point."""

    def test_decode_le_default(self) -> None:
        samples = _sine_samples(160)
        b64 = _make_pcm16_le_b64(samples)
        result = decode_pcm_audio_chunk(b64, sample_rate_hz=16000)
        np.testing.assert_array_equal(result, samples)

    def test_decode_be_explicit(self) -> None:
        samples = _sine_samples(80)
        b64 = _make_pcm16_be_b64(samples)
        result = decode_pcm_audio_chunk(b64, encoding="pcm16_be", sample_rate_hz=16000)
        np.testing.assert_array_equal(result, samples)

    def test_opus_raises_unknown_encoding(self) -> None:
        b64 = base64.b64encode(b"\x00\x00").decode()
        with pytest.raises(DecoderUnknownEncoding, match="Opus"):
            decode_pcm_audio_chunk(b64, encoding="opus", sample_rate_hz=16000)

    def test_unknown_encoding_raises(self) -> None:
        b64 = base64.b64encode(b"\x00\x00").decode()
        with pytest.raises(DecoderUnknownEncoding, match="Unknown"):
            decode_pcm_audio_chunk(b64, encoding="aac", sample_rate_hz=16000)

    def test_non_vad_rate_raises(self) -> None:
        b64 = _make_pcm16_le_b64(_sine_samples(100))
        with pytest.raises(AudioDecodeError, match="not one of"):
            decode_pcm_audio_chunk(b64, sample_rate_hz=44100)

    def test_none_rate_accepted(self) -> None:
        """sample_rate_hz=None should not error — callers validate later."""
        b64 = _make_pcm16_le_b64(_sine_samples(10))
        result = decode_pcm_audio_chunk(b64, sample_rate_hz=None)
        assert len(result) == 10

    def test_encoding_aliases(self) -> None:
        """Common aliases for pcm16_le should resolve correctly."""
        samples = _sine_samples(20)
        b64 = _make_pcm16_le_b64(samples)
        for alias in ("pcm_le", "s16_le", "signed16_le", "PCM16_LE"):
            result = decode_pcm_audio_chunk(b64, encoding=alias, sample_rate_hz=16000)
            np.testing.assert_array_equal(result, samples)


class TestResampleToVadRate:
    """Test the linear-interpolation resampler."""

    def test_same_rate_returns_copy(self) -> None:
        samples = _sine_samples(160, sample_rate=16000)
        result = resample_to_vad_rate(samples, from_rate=16000, to_rate=16000)
        np.testing.assert_array_equal(result, samples)
        assert result is not samples  # Must be a copy

    def test_upsample_8k_to_16k(self) -> None:
        samples = _sine_samples(80, sample_rate=8000)
        result = resample_to_vad_rate(samples, from_rate=8000, to_rate=16000)
        assert len(result) == 160  # 2× upsample

    def test_downsample_32k_to_16k(self) -> None:
        samples = _sine_samples(320, sample_rate=32000)
        result = resample_to_vad_rate(samples, from_rate=32000, to_rate=16000)
        assert len(result) == 160

    def test_48k_to_16k(self) -> None:
        samples = _sine_samples(480, sample_rate=48000)
        result = resample_to_vad_rate(samples, from_rate=48000, to_rate=16000)
        assert len(result) == 160

    def test_empty_input(self) -> None:
        samples = np.array([], dtype=np.int16)
        result = resample_to_vad_rate(samples, from_rate=8000, to_rate=16000)
        assert len(result) == 0

    def test_single_sample(self) -> None:
        samples = np.array([1000], dtype=np.int16)
        result = resample_to_vad_rate(samples, from_rate=8000, to_rate=16000)
        assert len(result) == 2
        assert result[0] == 1000
        assert result[1] == 1000

    def test_invalid_from_rate(self) -> None:
        with pytest.raises(AudioDecodeError, match="from_rate"):
            resample_to_vad_rate(np.array([0], dtype=np.int16),
                                 from_rate=44100, to_rate=16000)

    def test_invalid_to_rate(self) -> None:
        with pytest.raises(AudioDecodeError, match="to_rate"):
            resample_to_vad_rate(np.array([0], dtype=np.int16),
                                 from_rate=16000, to_rate=44100)

    def test_output_dtype_int16(self) -> None:
        samples = _sine_samples(80, sample_rate=8000)
        result = resample_to_vad_rate(samples, from_rate=8000, to_rate=16000)
        assert result.dtype == np.int16

    def test_clipping(self) -> None:
        """Extreme values must stay within int16 range after resample."""
        samples = np.array([32767, -32768, 32767, -32768], dtype=np.int16)
        result = resample_to_vad_rate(samples, from_rate=8000, to_rate=16000)
        assert result.max() <= 32767
        assert result.min() >= -32768


class TestSplitIntoVadFrames:
    """Test frame splitting for webrtcvad."""

    def test_exact_multiple(self) -> None:
        """320 samples = 2 × 160 (10 ms @ 16 kHz) → 2 frames, no padding."""
        samples = _sine_samples(320, sample_rate=16000)
        frames = split_into_vad_frames(
            samples, sample_rate_hz=16000, duration_ms=10
        )
        assert len(frames) == 2
        assert all(len(f.samples) == 160 for f in frames)
        assert frames[0].captured_at_ms == 0
        assert frames[1].captured_at_ms == 10

    def test_with_tail_pad(self) -> None:
        """200 samples → 1 full frame (160) + 1 padded frame (40 real + 120 zeros)."""
        samples = _sine_samples(200, sample_rate=16000)
        frames = split_into_vad_frames(
            samples, sample_rate_hz=16000, duration_ms=10
        )
        assert len(frames) == 2
        # First frame: all real samples
        assert len(frames[0].samples) == 160
        # Second frame: padded to 160
        assert len(frames[1].samples) == 160
        # The tail (last 120 samples) should be zeros
        np.testing.assert_array_equal(frames[1].samples[40:], 0)

    def test_empty_input(self) -> None:
        samples = np.array([], dtype=np.int16)
        frames = split_into_vad_frames(
            samples, sample_rate_hz=16000, duration_ms=10
        )
        assert len(frames) == 0

    def test_20ms_frames(self) -> None:
        """320 samples = 1 × 320 (20 ms @ 16 kHz) → 1 frame."""
        samples = _sine_samples(320, sample_rate=16000)
        frames = split_into_vad_frames(
            samples, sample_rate_hz=16000, duration_ms=20
        )
        assert len(frames) == 1
        assert len(frames[0].samples) == 320

    def test_30ms_frames(self) -> None:
        """480 samples = 1 × 480 (30 ms @ 16 kHz) → 1 frame."""
        samples = _sine_samples(480, sample_rate=16000)
        frames = split_into_vad_frames(
            samples, sample_rate_hz=16000, duration_ms=30
        )
        assert len(frames) == 1
        assert len(frames[0].samples) == 480

    def test_captured_at_offset(self) -> None:
        samples = _sine_samples(320, sample_rate=16000)
        frames = split_into_vad_frames(
            samples, sample_rate_hz=16000, duration_ms=10,
            captured_at_offset_ms=500,
        )
        assert frames[0].captured_at_ms == 500
        assert frames[1].captured_at_ms == 510

    def test_invalid_rate_raises(self) -> None:
        with pytest.raises(AudioDecodeError, match="not VAD-supported"):
            split_into_vad_frames(
                np.array([0], dtype=np.int16),
                sample_rate_hz=44100, duration_ms=10,
            )

    def test_invalid_duration_raises(self) -> None:
        with pytest.raises(AudioDecodeError, match="not VAD-supported"):
            split_into_vad_frames(
                np.array([0], dtype=np.int16),
                sample_rate_hz=16000, duration_ms=15,
            )

    def test_frame_metadata(self) -> None:
        samples = _sine_samples(160, sample_rate=16000)
        frames = split_into_vad_frames(
            samples, sample_rate_hz=16000, duration_ms=10
        )
        assert frames[0].sample_rate_hz == 16000
        assert frames[0].duration_ms == 10

    def test_8khz_10ms(self) -> None:
        """80 samples = 1 × 80 (10 ms @ 8 kHz) → 1 frame."""
        samples = _sine_samples(80, sample_rate=8000)
        frames = split_into_vad_frames(
            samples, sample_rate_hz=8000, duration_ms=10
        )
        assert len(frames) == 1
        assert len(frames[0].samples) == 80


class TestComputeRmsDb:
    """Test the RMS dBFS calculator."""

    def test_empty_returns_neg_inf(self) -> None:
        result = compute_rms_db(np.array([], dtype=np.int16))
        assert result == float("-inf")

    def test_all_zeros_returns_neg_inf(self) -> None:
        result = compute_rms_db(np.zeros(100, dtype=np.int16))
        assert result == float("-inf")

    def test_full_scale_sine_near_zero_db(self) -> None:
        """Full-scale sine (±32767) → RMS ≈ 32767/√2 → ~ -3.01 dBFS."""
        t = np.arange(16000, dtype=np.float64) / 16000  # 1 second
        sine = (np.sin(2 * np.pi * 1000 * t) * 32767).astype(np.int16)
        rms = compute_rms_db(sine)
        assert -3.5 < rms < -2.5, f"Expected ~-3.01, got {rms}"

    def test_quiet_signal_is_negative(self) -> None:
        """A low-amplitude signal has a large negative dBFS."""
        quiet = np.full(160, 10, dtype=np.int16)
        rms = compute_rms_db(quiet)
        assert rms < -60, f"Expected < -60, got {rms}"

    def test_dc_offset(self) -> None:
        """Constant DC offset → RMS = that constant → known dBFS."""
        dc = np.full(100, 3277, dtype=np.int16)  # ≈ 10% full scale
        rms = compute_rms_db(dc)
        expected = 20.0 * math.log10(3277 / 32768.0)
        assert abs(rms - expected) < 0.1

    def test_single_sample(self) -> None:
        rms = compute_rms_db(np.array([16384], dtype=np.int16))
        expected = 20.0 * math.log10(16384 / 32768.0)
        assert abs(rms - expected) < 0.01


class TestPreprocessAudioChunk:
    """Test the end-to-end audio pipeline."""

    def test_basic_pipeline(self) -> None:
        """Full pipeline: encode → decode → split → RMS."""
        samples = _sine_samples(320, sample_rate=16000)
        b64 = _make_pcm16_le_b64(samples)
        frames, rms = preprocess_audio_chunk(
            b64,
            sample_rate_hz=16000,
            chunk_duration_ms=20,
            frame_duration_ms=10,
        )
        assert len(frames) == 2
        assert all(isinstance(f, AudioFrame) for f in frames)
        assert isinstance(rms, float)
        assert rms > float("-inf")

    def test_invalid_frame_duration_raises(self) -> None:
        b64 = _make_pcm16_le_b64(_sine_samples(160))
        with pytest.raises(AudioDecodeError, match="not VAD-supported"):
            preprocess_audio_chunk(
                b64,
                sample_rate_hz=16000,
                chunk_duration_ms=10,
                frame_duration_ms=15,
            )


class TestNearestVadRate:
    """Test the _nearest_vad_rate helper."""

    def test_12000_maps_to_16000(self) -> None:
        assert _nearest_vad_rate(12000) == 16000

    def test_exact_match(self) -> None:
        for rate in VAD_SAMPLE_RATES:
            assert _nearest_vad_rate(rate) == rate

    def test_44100_maps_to_48000(self) -> None:
        assert _nearest_vad_rate(44100) == 48000

    def test_1000_maps_to_8000(self) -> None:
        assert _nearest_vad_rate(1000) == 8000

    def test_24000_tie_breaks_higher(self) -> None:
        """24000 is equidistant from 16000 and 32000; tie breaks to higher."""
        result = _nearest_vad_rate(24000)
        assert result == 32000


# ======================================================================
# Section 3 — scheduler.py
# ======================================================================


class TestModalitySchedulerDefaults:
    """Test the scheduler with default periods."""

    def test_default_periods(self) -> None:
        sched = ModalityScheduler()
        periods = sched.periods
        assert periods[InferenceModality.HEAD_POSE_GAZE] == 1
        assert periods[InferenceModality.OBJECT_DETECTION] == 1
        assert periods[InferenceModality.IDENTITY_MATCH] == 5

    def test_frame_0_runs_all(self) -> None:
        """Frame 0 is a multiple of every period, so all modalities run."""
        sched = ModalityScheduler()
        decision = sched.decide_for_frame(0)
        assert len(decision.modalities_to_run()) == 3
        assert len(decision.modalities_to_skip()) == 0

    def test_frame_1_skips_identity(self) -> None:
        sched = ModalityScheduler()
        decision = sched.decide_for_frame(1)
        to_run = decision.modalities_to_run()
        assert InferenceModality.HEAD_POSE_GAZE in to_run
        assert InferenceModality.OBJECT_DETECTION in to_run
        assert InferenceModality.IDENTITY_MATCH not in to_run

    def test_frame_5_runs_all(self) -> None:
        sched = ModalityScheduler()
        decision = sched.decide_for_frame(5)
        assert InferenceModality.IDENTITY_MATCH in decision.modalities_to_run()

    def test_frame_seq_stored(self) -> None:
        sched = ModalityScheduler()
        decision = sched.decide_for_frame(42)
        assert decision.frame_seq == 42


class TestModalitySchedulerCustom:
    """Test the scheduler with custom periods."""

    def test_custom_identity_period(self) -> None:
        sched = ModalityScheduler(identity_match_period=3)
        assert sched.period_for(InferenceModality.IDENTITY_MATCH) == 3
        assert sched.decide_for_frame(3).should_run(InferenceModality.IDENTITY_MATCH)
        assert not sched.decide_for_frame(2).should_run(InferenceModality.IDENTITY_MATCH)

    def test_all_skip_on_non_multiples(self) -> None:
        sched = ModalityScheduler(
            head_pose_period=2,
            object_detection_period=3,
            identity_match_period=5,
        )
        decision = sched.decide_for_frame(1)
        assert len(decision.modalities_to_run()) == 0
        assert len(decision.modalities_to_skip()) == 3

    def test_period_for(self) -> None:
        sched = ModalityScheduler(head_pose_period=4)
        assert sched.period_for(InferenceModality.HEAD_POSE_GAZE) == 4


class TestModalitySchedulerValidation:
    """Test that invalid scheduler arguments raise."""

    def test_zero_head_pose_period(self) -> None:
        with pytest.raises(ValueError, match="head_pose_period"):
            ModalityScheduler(head_pose_period=0)

    def test_negative_object_detection_period(self) -> None:
        with pytest.raises(ValueError, match="object_detection_period"):
            ModalityScheduler(object_detection_period=-1)

    def test_zero_identity_match_period(self) -> None:
        with pytest.raises(ValueError, match="identity_match_period"):
            ModalityScheduler(identity_match_period=0)

    def test_negative_frame_seq(self) -> None:
        sched = ModalityScheduler()
        with pytest.raises(ValueError, match="non-negative"):
            sched.decide_for_frame(-1)


class TestInferenceDecision:
    """Test the InferenceDecision convenience methods."""

    def test_should_run_true(self) -> None:
        d = InferenceDecision(
            frame_seq=0,
            decisions=[
                ModalityDecision(
                    modality=InferenceModality.HEAD_POSE_GAZE,
                    decision=ScheduleDecision.RUN,
                    reason="test",
                ),
            ],
        )
        assert d.should_run(InferenceModality.HEAD_POSE_GAZE) is True

    def test_should_run_false_for_missing_modality(self) -> None:
        d = InferenceDecision(frame_seq=0, decisions=[])
        assert d.should_run(InferenceModality.HEAD_POSE_GAZE) is False

    def test_modalities_to_skip(self) -> None:
        d = InferenceDecision(
            frame_seq=1,
            decisions=[
                ModalityDecision(
                    modality=InferenceModality.IDENTITY_MATCH,
                    decision=ScheduleDecision.SKIP,
                    reason="not a multiple",
                ),
            ],
        )
        assert InferenceModality.IDENTITY_MATCH in d.modalities_to_skip()


class TestScheduleDecisionEnum:
    """Test the ScheduleDecision enum values."""

    def test_values(self) -> None:
        assert ScheduleDecision.RUN.value == "run"
        assert ScheduleDecision.SKIP.value == "skip"


class TestInferenceModalityEnum:
    """Test the InferenceModality enum values."""

    def test_values(self) -> None:
        assert InferenceModality.HEAD_POSE_GAZE.value == "head_pose_gaze"
        assert InferenceModality.OBJECT_DETECTION.value == "object_detection"
        assert InferenceModality.IDENTITY_MATCH.value == "identity_match"

    def test_all_three(self) -> None:
        assert len(InferenceModality) == 3


# ======================================================================
# Section 4 — rolling_buffer.py
# ======================================================================


def _make_entry(
    *,
    seconds_offset: int = 0,
    size_bytes: int = 1000,
    encoded: str = "AAAA",
) -> RollingBufferEntry:
    """Helper to create a RollingBufferEntry with sensible defaults."""
    return RollingBufferEntry(
        captured_at=datetime(2026, 7, 23, 12, 0, seconds_offset, tzinfo=timezone.utc),
        encoded=encoded,
        encoding="jpeg",
        resolution=(640, 480),
        size_bytes=size_bytes,
    )


class TestRollingBufferConfig:
    """Test RollingBufferConfig validation."""

    def test_defaults(self) -> None:
        cfg = RollingBufferConfig()
        assert cfg.capture_interval_ms == 300
        assert cfg.window_seconds == 15
        assert cfg.max_entries == 60

    def test_capture_interval_below_min(self) -> None:
        with pytest.raises(ValueError, match="capture_interval_ms.*outside"):
            RollingBufferConfig(capture_interval_ms=100)

    def test_capture_interval_above_max(self) -> None:
        with pytest.raises(ValueError, match="capture_interval_ms.*outside"):
            RollingBufferConfig(capture_interval_ms=600)

    def test_window_below_min(self) -> None:
        with pytest.raises(ValueError, match="window_seconds.*outside"):
            RollingBufferConfig(window_seconds=5)

    def test_window_above_max(self) -> None:
        with pytest.raises(ValueError, match="window_seconds.*outside"):
            RollingBufferConfig(window_seconds=20)

    def test_max_entries_zero(self) -> None:
        with pytest.raises(ValueError, match="max_entries"):
            RollingBufferConfig(max_entries=0)

    def test_max_bytes_per_entry_zero(self) -> None:
        with pytest.raises(ValueError, match="max_bytes_per_entry"):
            RollingBufferConfig(max_bytes_per_entry=0)

    def test_max_total_bytes_zero(self) -> None:
        with pytest.raises(ValueError, match="max_total_bytes"):
            RollingBufferConfig(max_total_bytes=0)

    def test_boundary_values_accepted(self) -> None:
        """Edge values at the boundaries should be accepted."""
        cfg = RollingBufferConfig(capture_interval_ms=200, window_seconds=10)
        assert cfg.capture_interval_ms == 200
        assert cfg.window_seconds == 10

        cfg2 = RollingBufferConfig(capture_interval_ms=500, window_seconds=15)
        assert cfg2.capture_interval_ms == 500
        assert cfg2.window_seconds == 15

    def test_frozen(self) -> None:
        cfg = RollingBufferConfig()
        with pytest.raises(AttributeError):
            cfg.capture_interval_ms = 250  # type: ignore[misc]


class TestRollingBufferEntry:
    """Test RollingBufferEntry validation."""

    def test_valid_entry(self) -> None:
        entry = _make_entry()
        assert entry.encoding == "jpeg"
        assert entry.resolution == (640, 480)

    def test_negative_size_raises(self) -> None:
        with pytest.raises(BufferFlushError, match="negative"):
            _make_entry(size_bytes=-1)

    def test_unsupported_encoding_raises(self) -> None:
        with pytest.raises(BufferFlushError, match="not supported"):
            RollingBufferEntry(
                captured_at=datetime.now(timezone.utc),
                encoded="AAAA",
                encoding="bmp",
                resolution=(640, 480),
                size_bytes=100,
            )

    def test_empty_encoded_raises(self) -> None:
        with pytest.raises(BufferFlushError, match="empty"):
            RollingBufferEntry(
                captured_at=datetime.now(timezone.utc),
                encoded="",
                encoding="jpeg",
                resolution=(640, 480),
                size_bytes=100,
            )

    def test_frozen(self) -> None:
        entry = _make_entry()
        with pytest.raises(AttributeError):
            entry.size_bytes = 999  # type: ignore[misc]


class TestInMemoryRollingBuffer:
    """Test the in-memory RollingBuffer implementation."""

    def test_append_and_len(self) -> None:
        buf = InMemoryRollingBuffer(session_id="s1")
        assert len(buf) == 0
        buf.append(_make_entry(seconds_offset=0))
        assert len(buf) == 1
        buf.append(_make_entry(seconds_offset=1))
        assert len(buf) == 2

    def test_eviction_at_max_entries(self) -> None:
        cfg = RollingBufferConfig(max_entries=3)
        buf = InMemoryRollingBuffer(session_id="s1", config=cfg)
        for i in range(5):
            buf.append(_make_entry(seconds_offset=i))
        assert len(buf) == 3
        # The oldest entries (0, 1) should be evicted.
        timestamps = [e.captured_at.second for e in buf]
        assert timestamps == [2, 3, 4]

    def test_window_start_and_end(self) -> None:
        buf = InMemoryRollingBuffer(session_id="s1")
        assert buf.window_start() is None
        assert buf.window_end() is None
        buf.append(_make_entry(seconds_offset=5))
        buf.append(_make_entry(seconds_offset=10))
        assert buf.window_start() is not None
        assert buf.window_start().second == 5  # type: ignore[union-attr]
        assert buf.window_end().second == 10  # type: ignore[union-attr]

    def test_total_bytes(self) -> None:
        buf = InMemoryRollingBuffer(session_id="s1")
        buf.append(_make_entry(size_bytes=100))
        buf.append(_make_entry(size_bytes=200, seconds_offset=1))
        assert buf.total_bytes() == 300

    def test_iteration(self) -> None:
        buf = InMemoryRollingBuffer(session_id="s1")
        for i in range(3):
            buf.append(_make_entry(seconds_offset=i))
        entries = list(buf)
        assert len(entries) == 3
        assert all(isinstance(e, RollingBufferEntry) for e in entries)

    def test_oversized_entry_raises(self) -> None:
        cfg = RollingBufferConfig(max_bytes_per_entry=10)
        buf = InMemoryRollingBuffer(session_id="s1", config=cfg)
        # "AAAA" decodes to 3 bytes so fits; a long payload won't.
        big = base64.b64encode(b"\x00" * 100).decode()
        with pytest.raises(BufferFlushError, match="exceeds max_bytes_per_entry"):
            buf.append(_make_entry(encoded=big))

    def test_to_serializable(self) -> None:
        buf = InMemoryRollingBuffer(session_id="s1")
        buf.append(_make_entry(seconds_offset=0))
        d = buf.to_serializable()
        assert d["session_id"] == "s1"
        assert d["captured_window_start"] is not None
        assert d["captured_window_end"] is not None
        assert len(d["entries"]) == 1
        entry = d["entries"][0]
        assert "captured_at" in entry
        assert "frame" in entry
        assert "encoding" in entry
        assert "resolution" in entry
        assert "size_bytes" in entry

    def test_to_serializable_empty(self) -> None:
        buf = InMemoryRollingBuffer(session_id="s1")
        d = buf.to_serializable()
        assert d["captured_window_start"] is None
        assert d["captured_window_end"] is None
        assert d["entries"] == []


class TestNullRollingBuffer:
    """Test the no-op RollingBuffer."""

    def test_append_is_noop(self) -> None:
        buf = NullRollingBuffer(session_id="null-test")
        buf.append(_make_entry())
        assert len(buf) == 0

    def test_window_methods(self) -> None:
        buf = NullRollingBuffer()
        assert buf.window_start() is None
        assert buf.window_end() is None
        assert buf.total_bytes() == 0

    def test_iteration_empty(self) -> None:
        buf = NullRollingBuffer()
        assert list(buf) == []

    def test_to_serializable(self) -> None:
        buf = NullRollingBuffer(session_id="ns")
        d = buf.to_serializable()
        assert d["session_id"] == "ns"
        assert d["entries"] == []


class TestRollingBufferProtocol:
    """Test that both implementations satisfy the RollingBuffer protocol."""

    def test_in_memory_is_rolling_buffer(self) -> None:
        buf = InMemoryRollingBuffer(session_id="s1")
        assert isinstance(buf, RollingBuffer)

    def test_null_is_rolling_buffer(self) -> None:
        buf = NullRollingBuffer()
        assert isinstance(buf, RollingBuffer)


class TestApproxDecodedSize:
    """Test the _approx_decoded_size helper."""

    def test_empty_string(self) -> None:
        assert _approx_decoded_size("") == 0

    def test_no_padding(self) -> None:
        # "AAAA" = 4 chars, 0 padding → ceil(4 * 3/4) = 3 bytes
        assert _approx_decoded_size("AAAA") == 3

    def test_with_padding(self) -> None:
        # "AA==" = 4 chars, 2 padding → ceil((4-2)*3/4) = ceil(1.5) = 2
        assert _approx_decoded_size("AA==") == 2

    def test_known_payload(self) -> None:
        raw = b"\x00" * 100
        b64 = base64.b64encode(raw).decode()
        approx = _approx_decoded_size(b64)
        # Should be >= 100 (may be slightly over due to ceiling)
        assert approx >= 100
        assert approx <= 110  # Not wildly off


# ======================================================================
# Section 5 — package-level imports
# ======================================================================


class TestPackageExports:
    """Test that the top-level __init__.py exposes the public surface."""

    def test_frames_exports(self) -> None:
        from proctoring_engine.preprocessing import (
            ACCEPTED_ENCODINGS as _a,
            DecodedFrame as _b,
            FrameDecodeError as _c,
            decode_jpeg_frame as _d,
            normalize_for_mediapipe as _e,
            normalize_for_yolov8 as _f,
        )

    def test_audio_exports(self) -> None:
        from proctoring_engine.preprocessing import (
            AudioDecodeError as _a,
            AudioFrame as _b,
            DecoderPcm16Be as _c,
            DecoderPcm16Le as _d,
            DecoderUnknownEncoding as _e,
            VAD_FRAME_DURATIONS as _f,
            VAD_SAMPLE_RATES as _g,
            compute_rms_db as _h,
            decode_pcm_audio_chunk as _i,
            preprocess_audio_chunk as _j,
            resample_to_vad_rate as _k,
            split_into_vad_frames as _l,
        )

    def test_scheduler_exports(self) -> None:
        from proctoring_engine.preprocessing import (
            InferenceDecision as _a,
            InferenceModality as _b,
            ModalityDecision as _c,
            ModalityScheduler as _d,
            ScheduleDecision as _e,
        )

    def test_rolling_buffer_exports(self) -> None:
        from proctoring_engine.preprocessing import (
            BufferFlushError as _a,
            InMemoryRollingBuffer as _b,
            NullRollingBuffer as _c,
            RollingBuffer as _d,
            RollingBufferConfig as _e,
            RollingBufferEntry as _f,
        )
