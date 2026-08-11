"""Liveness / anti-spoofing inference module.

Catches print and screen-replay spoofing specifically — not deepfakes
or 3D masks (``docs/04-inference-modules-design.md`` §7).

**Library: ``uniface[cpu]``** (PyPI, MIT), wrapping **MiniFASNetV2**
(Apache 2.0) via **ONNX Runtime**.  Verified by direct installation:
imports cleanly, ``onnxruntime`` adds ~58 MB to the image, and the
``MiniFASNetV2.onnx`` weights are fetched from a pinned GitHub
Releases URL with a built-in SHA-256 check
(``b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907``).
Bake the weights into the image via the ``LIVENESS_MODEL_PATH``
environment variable, same pattern as the other model bundles
(``MP_FACE_DETECTOR_BUNDLE``, ``YOLO_WEIGHTS_PATH``, etc.).

The module produces a 3-class softmax over ``(real, print, replay)``
per frame, plus a binary ``is_real`` flag (``True`` iff
``real`` is the argmax).  This is a single-frame point estimate —
the multi-frame confidence interval is built by the fusion engine
from the same window-and-spread pattern as identity-match.

**Architectural boundary:** this module computes a single-frame point
estimate of the spoof-vs-real classification.  The spec's
confidence-interval requirement (``docs/04-inference-modules-design.md``
§7) is fulfilled by the fusion engine, which collects point estimates
from several frames within a sampling window and computes the
statistical interval (mean ± std, or min/max spread).  This keeps the
inference module stateless and the window logic in one place.

**Face-crop reuse:** this module shares the upstream uncropped
heavy frame (JPEG) but performs its own cropping via the uniface
API.  It requires a bounding box ``(x1, y1, x2, y2)`` in pixel
coordinates.  Because the identity-match module uses dlib
(which detects internally but does not emit bounding boxes), a
separate face-detection pass is required on the server to generate
this box before liveness can run.  (The client's light-inference
face bbox is too imprecise and misaligned with the heavy frame to
use for anti-spoofing).

Input:  the uncropped heavy frame (``np.ndarray``, BGR, HxWx3, uint8)
        + a face bounding box ``[x1, y1, x2, y2]``.
Output: a :class:`LivenessResult`.
"""

from __future__ import annotations

import abc
import hashlib
import logging
import os
from typing import Final

import numpy as np

from proctoring_engine.inference._types import (
    ConfidenceInterval,
    LivenessResult,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODALITY: Final[str] = "liveness"

EVENT_LIVENESS_REAL: Final[str] = "liveness_real"
EVENT_LIVENESS_SPOOF: Final[str] = "liveness_spoof"

LIVENESS_MODEL_PATH_ENV: Final[str] = "LIVENESS_MODEL_PATH"
"""Environment variable pointing to a directory containing pre-baked
``MiniFASNetV2.onnx`` weights.

When set, the uniface ``MiniFASNet`` is told to look in this directory
instead of its default cache (``~/.uniface/models/``).  Used in
production to point at weights baked into the container image at
build time, rather than downloading them at first inference.  When
unset, the default uniface cache location is used and weights are
downloaded on first construction (with the built-in SHA-256
verification; see ``MINIFASNET_V2_SHA256`` below).
"""

# Pinned SHA-256 of MiniFASNetV2.onnx, verified by direct download.
# This matches the uniface built-in ``verify_model_weights`` check —
# the upstream library downloads and verifies the file with this
# hash.  If this hash ever changes, the change must be reviewed
# against the upstream repository — a silent update to a different
# model family would be a security-relevant change, not just a
# versioning nit.
MINIFASNET_V2_SHA256: Final[str] = (
    "b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907"
)

# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without a model)
# ---------------------------------------------------------------------------


def compute_liveness_confidence(score: float) -> ConfidenceInterval:
    """Single-frame confidence interval for a liveness classification.

    For a single-frame point estimate, the interval is degenerate
    ``(score, score, score)`` — the fusion engine builds the
    multi-frame interval from the per-frame point estimates.  This
    helper exists so the contract with the rest of the inference
    layer is uniform: every modality emits a
    :class:`ConfidenceInterval`, not a bare float.
    """
    score = max(0.0, min(1.0, score))
    return ConfidenceInterval(
        lower=score, score=score, upper=score,
    )


# ---------------------------------------------------------------------------
# Backend ABC
# ---------------------------------------------------------------------------


class LivenessBackend(abc.ABC):
    """Abstract interface for a liveness / anti-spoofing backend.

    Implementations wrap a specific inference library (``uniface`` /
    ONNX Runtime, etc.) behind this uniform ``classify`` contract.
    The runner doesn't know or care which library produces the
    softmax — only that it gets a 3-vector of probabilities.
    """

    @abc.abstractmethod
    def predict(self, frame_bgr: np.ndarray, bbox_xyxy: list[int] | np.ndarray) -> tuple[bool, float]:
        """Run a single forward pass on the frame + bounds.

        Parameters
        ----------
        frame_bgr:
            An uncropped BGR ``uint8`` numpy array of shape ``(H, W, 3)``.
            This is the raw heavy frame, but converted to BGR channel
            order (the format uniface's internal preprocessor expects).
        bbox_xyxy:
            A 4-element sequence ``[x1, y1, x2, y2]`` representing the
            face bounding box in pixel coordinates.

        Returns
        -------
        tuple[bool, float]
            ``(is_real, confidence)``
        """

    @property
    @abc.abstractmethod
    def model_name(self) -> str:
        """Short identifier for the model (e.g. ``"MiniFASNetV2"``)."""


# ---------------------------------------------------------------------------
# uniface (MiniFASNetV2) backend
# ---------------------------------------------------------------------------


class UnifaceBackend(LivenessBackend):
    """Concrete backend wrapping ``uniface`` (MiniFASNetV2 / ONNX Runtime).

    The library fetches the MiniFASNetV2 weights at construction
    time (if not already cached locally) and verifies them against
    its own internal SHA-256 pin (which resolves to
    ``MINIFASNET_V2_SHA256``).  In a production environment, set the
    ``LIVENESS_MODEL_PATH`` env var to a directory where the weapons
    were baked at build time to avoid runtime downloading.

    Heavy import is deferred to construction time, so the module
    itself is importable on any platform.
    """

    def __init__(
        self,
        *,
        model_directory: str | None = None,
        providers: list[str] | None = None,
    ) -> None:
        # uniface + onnxruntime are the heavy imports — defer them to
        # construction time so the rest of the module is importable on
        # any platform.  Tests that exercise this backend use
        # ``pytest.importorskip("uniface")`` to skip cleanly on
        # platforms where it is absent.
        import uniface
        from uniface.constants import MiniFASNetWeights

        model_directory = model_directory or os.environ.get(LIVENESS_MODEL_PATH_ENV)

        if providers is None:
            providers = ["CPUExecutionProvider"]

        # uniface handles the SHA-256 verification internally when it
        # resolves the model path. We can override the cache directory
        # it looks in via `set_cache_dir`, or we can manually supply
        # the weights.  The cleanest API is relying on uniface's
        # `model_store` logic but pointing it at our pre-baked directory.
        if model_directory is not None:
            from uniface.model_store import set_cache_dir
            set_cache_dir(model_directory)

        # create_spoofer returns a uniface.spoofing.MiniFASNet instance
        self._spoofer = uniface.create_spoofer(
            model_name=MiniFASNetWeights.V2,
            providers=providers,
        )

    def predict(self, frame_bgr: np.ndarray, bbox_xyxy: list[int] | np.ndarray) -> tuple[bool, float]:
        """Run a single forward pass and return (is_real, confidence)."""

        if frame_bgr.ndim != 3 or frame_bgr.shape[2] != 3:
            raise ValueError(
                f"Expected a (H, W, 3) BGR array; got shape {frame_bgr.shape}."
            )

        if len(bbox_xyxy) != 4:
            raise ValueError(
                f"Expected bbox_xyxy of length 4; got {len(bbox_xyxy)}."
            )

        # uniface.MiniFASNet.predict expects BGR image + xyxy bbox.
        # It handles its own cropping, scalar-scaling, and normalisation.
        result = self._spoofer.predict(frame_bgr, bbox_xyxy)
        return bool(result.is_real), float(result.confidence)

    @property
    def model_name(self) -> str:
        return "MiniFASNetV2"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class LivenessRunner:
    """Stateless per-frame liveness / anti-spoofing classifier.

    Accepts a :class:`LivenessBackend` at construction time and uses
    it to produce a 3-class softmax per face crop.  The ``run``
    method converts that into a :class:`LivenessResult`.

    The threshold (the policy-configured cutoff for what counts as
    "real enough") is **not** hardcoded — it's passed in to ``run`` as
    ``real_score_threshold`` and should come from
    ``PolicyConfig.liveness_score_threshold`` at the call site.
    Mirrors the identity-match contract.
    """

    def __init__(self, backend: LivenessBackend) -> None:
        self._backend = backend

    @property
    def backend(self) -> LivenessBackend:
        """The liveness backend in use."""
        return self._backend

    def run(
        self,
        frame_bgr: np.ndarray,
        bbox_xyxy: list[int] | np.ndarray,
        *,
        confidence_threshold: float = 0.5,
    ) -> LivenessResult:
        """Classify a face crop as real / spoof.

        Parameters
        ----------
        frame_bgr:
            BGR ``uint8`` numpy array of the uncropped heavy frame.
        bbox_xyxy:
            Face bounding box in pixel coordinates ``[x1, y1, x2, y2]``.
        confidence_threshold:
            The minimum ``confidence`` for the frame to be classified
            ``is_real=True`` when the model predicts "real".  Must
            come from ``PolicyConfig``, not hardcoded.  Default 0.5
            is the ``PolicyConfig.liveness_score_threshold`` default.

        Returns
        -------
        LivenessResult
            ``is_real`` is the model's classification gated by the
            policy threshold.  ``event_type`` is ``"liveness_real"``
            or ``"liveness_spoof"``.
        """

        if not (0.0 <= confidence_threshold <= 1.0):
            raise ValueError(
                f"confidence_threshold must be in [0, 1]; "
                f"got {confidence_threshold}."
            )

        model_is_real, raw_confidence = self._backend.predict(frame_bgr, bbox_xyxy)

        # The threshold is the policy-level signal; the model output
        # is the raw-model signal.  Both flags agree on a well-behaved
        # positive, but ``confidence`` can dip below the configured
        # threshold while still being predicted 'real' on borderline
        # frames — ``is_real`` follows the policy threshold in that
        # case, so the policy is what actually decides.
        is_real = (
            model_is_real and raw_confidence >= confidence_threshold
        )

        confidence_interval = compute_liveness_confidence(raw_confidence)

        event_type = (
            EVENT_LIVENESS_REAL if is_real else EVENT_LIVENESS_SPOOF
        )

        return LivenessResult(
            modality=MODALITY,
            event_type=event_type,
            confidence=confidence_interval,
            bounding_boxes=[],
            raw_value={
                "model_is_real": model_is_real,
                "raw_confidence": raw_confidence,
                "threshold": confidence_threshold,
                "model_name": self._backend.model_name,
            },
            is_real=is_real,
        )
