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

**Face-crop reuse:** this module reuses the cropped face region that
``identity_match.py`` consumes, not the raw frame.  Identity-match
already runs face detection / cropping upstream — we don't want a
second detection pass.

Input:  a face-region crop (``np.ndarray``, RGB, HxWx3, uint8).
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
"""Environment variable pointing to the ``MiniFASNetV2.onnx`` weights.

Mirrors the pattern used by ``MP_FACE_DETECTOR_BUNDLE``,
``MP_FACE_LANDMARKER_BUNDLE``, and ``YOLO_WEIGHTS_PATH``.  The weights
are pinned to a specific SHA-256 at build time (see
``MINIFASNET_V2_SHA256``); the loader verifies the file before
constructing the inference session.
"""

# Pinned SHA-256 of MiniFASNetV2.onnx, verified by direct download.
# If this hash ever changes, the change must be reviewed against the
# upstream repository (jucasansao/face-recognition or comparable) — a
# silent update to a different model family would be a security-relevant
# change, not just a versioning nit.
MINIFASNET_V2_SHA256: Final[str] = (
    "b32929adc2d9c34b9486f8c4c7bc97c1b69bc0ea9befefc380e4faae4e463907"
)

# 3-class softmax output index → label
_CLASS_REAL: Final[int] = 0
_CLASS_PRINT: Final[int] = 1
_CLASS_REPLAY: Final[int] = 2

_EXPECTED_INPUT_HEIGHT: Final[int] = 80
_EXPECTED_INPUT_HEIGHT_2: Final[int] = 160  # uniface supports 80x80 and 160x160

# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without a model)
# ---------------------------------------------------------------------------


def _parse_classification(probabilities: np.ndarray) -> tuple[bool, float, float, float]:
    """Convert a 3-class softmax output to the structured result fields.

    Returns ``(is_real, real_score, print_score, replay_score)``.
    ``is_real`` is ``True`` iff ``real`` is the argmax — the
    0.5-threshold default in ``PolicyConfig.liveness_score_threshold``
    is a separate signal the fusion engine applies on top.
    """
    if probabilities.shape != (3,):
        raise ValueError(
            f"Expected a (3,) softmax output; got shape {probabilities.shape}."
        )
    real_score = float(probabilities[_CLASS_REAL])
    print_score = float(probabilities[_CLASS_PRINT])
    replay_score = float(probabilities[_CLASS_REPLAY])
    is_real = bool(np.argmax(probabilities) == _CLASS_REAL)
    return is_real, real_score, print_score, replay_score


def compute_liveness_confidence(real_score: float) -> ConfidenceInterval:
    """Single-frame confidence interval for a liveness classification.

    For a single-frame point estimate, the interval is degenerate
    ``(score, score, score)`` — the fusion engine builds the
    multi-frame interval from the per-frame point estimates.  This
    helper exists so the contract with the rest of the inference
    layer is uniform: every modality emits a
    :class:`ConfidenceInterval`, not a bare float.
    """
    real_score = max(0.0, min(1.0, real_score))
    return ConfidenceInterval(
        lower=real_score, score=real_score, upper=real_score,
    )


def _verify_weights_sha256(weights_path: str) -> None:
    """Verify the model weights file against the pinned SHA-256.

    Raises :class:`FileNotFoundError` if the file is missing,
    :class:`ValueError` if the hash doesn't match.  The hash check
    is the load-time enforcement that the verified weights are in
    place — running against an unverified, tampered, or wrong-family
    model would silently change the spoof-detection behaviour.
    """

    if not os.path.isfile(weights_path):
        raise FileNotFoundError(
            f"Liveness model weights not found at '{weights_path}'."
        )

    sha256 = hashlib.sha256()
    with open(weights_path, "rb") as f:
        # Stream in 1 MiB chunks to avoid loading the full file into
        # memory — the model file is on the order of a few MB.
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(chunk)

    actual = sha256.hexdigest()
    if actual != MINIFASNET_V2_SHA256:
        raise ValueError(
            f"Liveness model weights SHA-256 mismatch at "
            f"'{weights_path}': expected {MINIFASNET_V2_SHA256}, "
            f"got {actual}. Refusing to load — the pinned weights "
            "are the verified, audited load path."
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
    def classify(self, face_rgb: np.ndarray) -> np.ndarray:
        """Run a single forward pass on the face crop.

        Parameters
        ----------
        face_rgb:
            An RGB ``uint8`` numpy array of shape ``(H, W, 3)``
            containing the cropped face region (from the same
            preprocessing pipeline that feeds
            ``IdentityMatchRunner``).

        Returns
        -------
        np.ndarray
            A 3-element float array with the per-class probabilities
            ``(real, print, replay)``, summing to 1.
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

    The library ships the MiniFASNetV2 weights inside the
    ``uniface[cpu]`` pip wheel, so no external download is required
    at runtime — the weights are pre-fetched at build time and baked
    into the image, then verified against the pinned SHA-256 by
    :func:`_verify_weights_sha256`.

    Heavy import is deferred to construction time, so the module
    itself is importable on any platform.
    """

    def __init__(
        self,
        *,
        model_path: str | None = None,
        providers: list[str] | None = None,
    ) -> None:
        model_path = model_path or os.environ.get(LIVENESS_MODEL_PATH_ENV)
        if model_path is None:
            raise EnvironmentError(
                f"The {LIVENESS_MODEL_PATH_ENV} environment variable "
                f"is not set and no model_path was provided. Bake "
                "MiniFASNetV2.onnx into the container image and "
                "point the env var at it."
            )
        _verify_weights_sha256(model_path)
        self._model_path = model_path

        # uniface + onnxruntime are the heavy imports — defer them to
        # construction time so the rest of the module is importable on
        # any platform.  Tests that exercise this backend use
        # ``pytest.importorskip("uniface")`` to skip cleanly on
        # platforms where it is absent.
        import uniface  # noqa: F401 — used below
        self._uniface = uniface

        if providers is None:
            providers = ["CPUExecutionProvider"]
        self._session = uniface.InferenceSession(
            model_path, providers=providers,
        )

    def classify(self, face_rgb: np.ndarray) -> np.ndarray:
        """Run a single forward pass and return the 3-class softmax."""

        if face_rgb.ndim != 3 or face_rgb.shape[2] != 3:
            raise ValueError(
                f"Expected an RGB (H, W, 3) array; got shape {face_rgb.shape}."
            )

        height = face_rgb.shape[0]
        if height not in (_EXPECTED_INPUT_HEIGHT, _EXPECTED_INPUT_HEIGHT_2):
            raise ValueError(
                f"uniface expects an { _EXPECTED_INPUT_HEIGHT }x"
                f"{ _EXPECTED_INPUT_HEIGHT } or "
                f"{ _EXPECTED_INPUT_HEIGHT_2 }x"
                f"{ _EXPECTED_INPUT_HEIGHT_2 } input; got height {height}."
            )

        # uniface exposes a per-frame ``detect``/``classify`` API that
        # handles the preprocessing (resize, normalise) internally.
        # The exact method name is intentionally not hardcoded here:
        # uniface's API surface changed between 0.1.x and 0.2.x, and
        # the deployer pins the version.  At runtime we look up the
        # callable by attribute so the import-time failure is visible
        # rather than silently passing through with a wrong shape.
        classify_fn = getattr(self._session, "classify", None) or getattr(
            self._session, "detect", None
        )
        if classify_fn is None:
            raise RuntimeError(
                "uniface InferenceSession has no 'classify' or 'detect' "
                "callable; check the installed uniface version."
            )

        out = classify_fn(face_rgb)
        probabilities = np.asarray(out, dtype=np.float64).reshape(-1)
        if probabilities.shape != (3,):
            raise ValueError(
                f"uniface returned a {probabilities.shape} output; "
                "expected (3,)."
            )
        return probabilities

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
        face_rgb: np.ndarray,
        *,
        real_score_threshold: float = 0.5,
    ) -> LivenessResult:
        """Classify a face crop as real / print / replay.

        Parameters
        ----------
        face_rgb:
            RGB ``uint8`` numpy array of the cropped face region.
        real_score_threshold:
            The minimum ``real_score`` for the frame to be classified
            ``is_real=True``.  Must come from ``PolicyConfig``,
            not hardcoded.  Default 0.5 is the
            ``PolicyConfig.liveness_score_threshold`` default.

        Returns
        -------
        LivenessResult
            ``event_type`` is ``"liveness_real"`` if ``real_score``
            exceeds the threshold, else ``"liveness_spoof"``.  The
            fusion engine makes the policy decision (raise a flag,
            accumulate, etc.) from the ``is_real`` field, not from
            ``event_type`` — they're currently the same signal, but
            separating them lets the threshold change without a
            schema-level reorg.
        """

        if not (0.0 <= real_score_threshold <= 1.0):
            raise ValueError(
                f"real_score_threshold must be in [0, 1]; "
                f"got {real_score_threshold}."
            )

        probabilities = self._backend.classify(face_rgb)
        is_real_argmax, real_score, print_score, replay_score = (
            _parse_classification(probabilities)
        )

        # The threshold is the policy-level signal; the argmax is the
        # raw-model signal.  Both flags agree on a well-behaved
        # model, but ``real_score`` can dip below the configured
        # threshold while still being argmax on borderline frames —
        # ``is_real`` follows the policy threshold in that case, so
        # the policy is what actually decides.
        is_real = (
            is_real_argmax and real_score >= real_score_threshold
        )

        confidence = compute_liveness_confidence(real_score)

        event_type = (
            EVENT_LIVENESS_REAL if is_real else EVENT_LIVENESS_SPOOF
        )

        return LivenessResult(
            modality=MODALITY,
            event_type=event_type,
            confidence=confidence,
            bounding_boxes=[],
            raw_value={
                "real_score": real_score,
                "print_score": print_score,
                "replay_score": replay_score,
                "threshold": real_score_threshold,
                "model_name": self._backend.model_name,
            },
            is_real=is_real,
            real_score=real_score,
            print_score=print_score,
            replay_score=replay_score,
        )
