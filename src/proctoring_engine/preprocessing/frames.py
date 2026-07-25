"""Frame decode and per-model normalisation.

The ingestion layer receives ``telemetry_heavy_frame`` envelopes with
base64-encoded JPEG / PNG / WebP data (per the WebSocket protocol).
This module turns those encoded bytes into a numeric array suitable
for downstream inference, and applies the *minimal* per-model
preprocessing each consumer requires.

**Per-model normalisation rules** (from
``docs/03-preprocessing-layer-design.md`` §1):

- **MediaPipe models** (face-presence / face-mesh for gaze) expect
  **RGB** input.  ``cv2.imdecode`` returns images in *BGR* (OpenCV's
  internal default), so we apply ``cv2.cvtColor(..., BGR2RGB)``
  *here* — if a caller passes a BGR array straight to MediaPipe, the
  landmark coordinates land on the wrong channel.  This is the only
  normalisation this layer does for MediaPipe: it does not resize,
  it does not normalise pixel values.
- **YOLOv8** (Ultralytics) performs its own internal preprocess
  (resize / letterbox / pixel-value normalisation). The contract for
  this layer is *just* decode — handing YOLOv8 a correctly-decoded
  array.
- **Identity match** depends on the *cropped, aligned* face region
  coming out of the face-presence module, not on the raw frame.
  This module deliberately does *not* run independent crop logic
  here — identity-match waits for the face-presence bounding box.

Decoded frames are stored as ``numpy.ndarray`` (H, W, C) uint8 to
keep the in-memory shape identical to OpenCV's standard and to make
zero-copy handoff to MediaPipe / YOLOv8 straightforward.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any, Final

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Error surface
# ---------------------------------------------------------------------------

class FrameDecodeError(Exception):
    """Raised whenever a frame payload can't be decoded."""

    def __init__(self, message: str, *, payload_kind: str | None = None) -> None:
        super().__init__(message)
        self.payload_kind = payload_kind

    def __repr__(self) -> str:
        # Keep the repr stable for tests that assert against it.
        return (
            f"FrameDecodeError({self.args[0]!r}, "
            f"payload_kind={self.payload_kind!r})"
        )


# ---------------------------------------------------------------------------
# Public decode constants
# ---------------------------------------------------------------------------

ACCEPTED_ENCODINGS: Final[frozenset[str]] = frozenset({"jpeg", "png", "webp"})

# Mapping from our high-level encoding name to the OpenCV
# ``ImreadFlags`` value.  Unchanged from OpenCV's defaults — explicit
# here so reviewers don't have to memorise them.
_OPENCV_IMREAD_FLAGS: Final[dict[str, int]] = {
    "jpeg": cv2.IMREAD_COLOR,
    "png": cv2.IMREAD_COLOR,
    "webp": cv2.IMREAD_COLOR,
}


# ---------------------------------------------------------------------------
# Decoded frame container
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class DecodedFrame:
    """A decoded image frame in numpy format.

    The frame is stored as (H, W, C) in the channel order requested by
    the caller — see :func:`decode_jpeg_frame` for the BGR default and
    :func:`normalize_for_mediapipe` for the RGB variant.
    """

    array: np.ndarray
    width: int
    height: int
    channels: int
    encoding: str


# ---------------------------------------------------------------------------
# Decode helpers
# ---------------------------------------------------------------------------

def _decode_base64_to_bytes(encoded: str, encoding: str) -> bytes:
    """Validate the base64 payload and return raw bytes.

    Raises :class:`FrameDecodeError` if the encoding isn't recognised
    or base64 decoding fails.
    """

    if encoding not in ACCEPTED_ENCODINGS:
        raise FrameDecodeError(
            f"encoding '{encoding}' is not supported; "
            f"use one of {sorted(ACCEPTED_ENCODINGS)}",
            payload_kind=encoding,
        )

    if not encoded or not encoded.strip():
        raise FrameDecodeError(
            "frame payload is empty",
            payload_kind=encoding,
        )

    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise FrameDecodeError(
            f"frame payload is not valid base64: {exc}",
            payload_kind=encoding,
        ) from exc


def decode_jpeg_frame(
    encoded: str,
    encoding: str = "jpeg",
) -> DecodedFrame:
    """Decode a base64-encoded image payload into a numpy array.

    Returns a :class:`DecodedFrame` in **BGR** channel order, which
    matches OpenCV's everywhere-format.  Callers that need RGB
    (MediaPipe) pass it to :func:`normalize_for_mediapipe`.
    Callers that hand the frame to YOLOv8 can use the BGR variant
    directly — Ultralytics converts internally.

    Raises :class:`FrameDecodeError` on any decode failure.
    """

    raw = _decode_base64_to_bytes(encoded, encoding)
    flags = _OPENCV_IMREAD_FLAGS[encoding]
    arr = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), flags)

    if arr is None:
        raise FrameDecodeError(
            "cv2.imdecode returned None for the supplied image; "
            "the bytes may be truncated or not a valid image.",
            payload_kind=encoding,
        )

    if arr.ndim != 3 or arr.shape[2] not in (3, 4):
        raise FrameDecodeError(
            f"decoded frame has unexpected shape {arr.shape!r}; "
            f"expected (H, W, 3) or (H, W, 4).",
            payload_kind=encoding,
        )

    height, width, channels = arr.shape
    return DecodedFrame(
        array=arr,
        width=width,
        height=height,
        channels=channels,
        encoding=encoding,
    )


# ---------------------------------------------------------------------------
# Per-model normalisation
# ---------------------------------------------------------------------------

def normalize_for_mediapipe(frame: DecodedFrame) -> DecodedFrame:
    """Convert a decoded BGR frame to RGB for MediaPipe models.

    The MediaPipe ``mp.solutions`` Face Detection and Face Mesh APIs
    expect **RGB** inputs.  Passing a BGR array to them silently
    produces landmarks computed against the wrong channel order — the
    exact bug this layer exists to prevent.

    Returns a *new* :class:`DecodedFrame` with the same metadata but
    with ``channels=3`` and ``encoding='rgb'``.  Frames with an alpha
    channel (RGBA / PNG with alpha) are blended onto a black
    background before the swap, since MediaPipe does not accept
    4-channel input.
    """

    src = frame.array
    if src.shape[2] == 4:
        src = cv2.cvtColor(src, cv2.COLOR_BGRA2BGR)
    rgb = cv2.cvtColor(src, cv2.COLOR_BGR2RGB)
    return DecodedFrame(
        array=rgb,
        width=frame.width,
        height=frame.height,
        channels=3,
        encoding="rgb",
    )


def normalize_for_yolov8(frame: DecodedFrame) -> DecodedFrame:
    """Return the frame unchanged for YOLOv8 handoff.

    Ultralytics performs its own resize / letterbox / pixel-value
    normalisation, so the honest contract for this layer is *just*
    decode.  This function exists so callers have a single
    dispatcher — every modality has a normalisation entry-point, even
    when that entry-point is a pass-through — and to make the
    intent explicit at the call site.
    """

    return frame


# ---------------------------------------------------------------------------
# Sanity check: ensure cv2 is the actual import (not a test stub).
# Done at module import time so a broken OpenCV install surfaces
# immediately, not at first decode attempt deep in inference.
# ---------------------------------------------------------------------------

def _verify_opencv_at_import() -> None:
    """Static sanity check that OpenCV is usable.

    Any failure here should block the service from starting, not
    surface later as mysterious decode errors during inference.
    """

    sample = np.zeros((8, 8, 3), dtype=np.uint8)
    sample[:, :, 2] = 255  # Pure red in BGR; should swap to purple in RGB.
    rgb = cv2.cvtColor(sample, cv2.COLOR_BGR2RGB)
    if rgb[0, 0, 0] != 255 or rgb[0, 0, 2] != 0:
        raise RuntimeError(
            "cv2.cvtColor BGR->RGB sanity check failed; "
            "check the OpenCV install."
        )


_verify_opencv_at_import()


# ---------------------------------------------------------------------------
# Test-only helper (not exported through ``__all__``)
# ---------------------------------------------------------------------------

def _is_opencv_array(obj: Any) -> bool:
    """Test utility: True if the object is a contiguous numpy uint8
    array in the canonical (H, W, 3) or (H, W, 4) shape.
    """

    return (
        isinstance(obj, np.ndarray)
        and obj.dtype == np.uint8
        and obj.ndim == 3
        and obj.shape[2] in (3, 4)
    )


_VERIFY = _verify_opencv_at_import  # Keep the reference alive for tests.
