"""Identity-match inference module.

Computes face embeddings and cosine similarity against the enrollment
reference.  The library choice is ``face_recognition`` (dlib ResNet,
128-dimensional embeddings) — resolved 2026-07-24 per explicit user
decision.

**Important architectural boundary:** this module computes a *single-
frame point estimate* of cosine similarity.  The spec's confidence-
interval requirement (``docs/04-inference-modules-design.md`` §2)
is fulfilled by the *fusion engine*, which collects point estimates
from several frames within a sampling window and computes the
statistical interval (mean ± std, or min/max spread).  This keeps
the inference module stateless and the window logic in one place.

**Packaging / ``pkg_resources`` fix:** ``face-recognition`` is
intentionally *not* listed in ``pyproject.toml`` dependencies.
The naive ``pip install face-recognition>=1.3`` breaks under
``setuptools>=82`` because pip resolves the dependency by
distribution name and pulls the broken PyPI
``face-recognition-models`` package, which uses the removed
``pkg_resources`` API.  The verified install sequence is:

1. ``pip install face_recognition==1.3.0 --no-deps``
2. ``pip install "click>=6.0" numpy "Pillow>=10.4,<12"``
3. ``pip install dlib-bin``  (prebuilt wheel — no MSVC needed)
4. ``pip install "git+https://github.com/jucasansao/face_recognition_models.git@35fd7aea15bfa1aa35532b102f7b408ab238b03d"``

This sequence is codified in the ``Dockerfile`` builder stage
(lines 62-80) and the CI workflow.  See ``pyproject.toml``
lines 29-44 for the rationale.

The heavy import is deferred to ``FaceRecognitionBackend``
construction, so the module is importable on any platform.
Tests that exercise the dlib backend use
``pytest.importorskip("face_recognition")`` to skip cleanly on
platforms where the library is absent.

Input:  a face-region crop (``np.ndarray``, RGB, HxWx3, uint8) +
        the enrollment embedding vector (``list[float]``).
Output: an :class:`IdentityMatchResult`.
"""

from __future__ import annotations

import abc
import logging
import math
from typing import Any, Final

import numpy as np

from proctoring_engine.inference._types import (
    ConfidenceInterval,
    IdentityMatchResult,
)


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MODALITY: Final[str] = "identity"

EVENT_IDENTITY_MATCH: Final[str] = "identity_match"
EVENT_IDENTITY_MISMATCH: Final[str] = "identity_mismatch"


# ---------------------------------------------------------------------------
# Cosine similarity (pure function, no library dependency)
# ---------------------------------------------------------------------------

def compute_cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two equal-length float vectors.

    Returns a value in ``[0, 1]``.  Vectors of zero magnitude return
    ``0.0`` rather than raising — a degenerate embedding should never
    be treated as a match.

    This function is intentionally implemented with ``numpy`` (already
    a project dependency) rather than ``scipy`` to avoid adding a
    transitive dependency solely for one dot-product.

    Parameters
    ----------
    a, b:
        Float vectors of equal length (e.g. 128-d dlib embeddings).

    Raises
    ------
    ValueError
        If the vectors have different lengths or are empty.
    """

    if len(a) != len(b):
        raise ValueError(
            f"Vectors must have equal length; got {len(a)} and {len(b)}."
        )
    if len(a) == 0:
        raise ValueError("Vectors must be non-empty.")

    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)

    dot = float(np.dot(va, vb))
    norm_a = float(np.linalg.norm(va))
    norm_b = float(np.linalg.norm(vb))

    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0

    # Cosine similarity is in [-1, 1]; clamp to [0, 1] because
    # negative similarity has no meaningful interpretation for face
    # embeddings (it would mean anti-correlated, which doesn't map
    # to identity).
    raw = dot / (norm_a * norm_b)
    return max(0.0, min(1.0, raw))


# ---------------------------------------------------------------------------
# Backend ABC
# ---------------------------------------------------------------------------

class IdentityBackend(abc.ABC):
    """Abstract interface for a face-embedding backend.

    Implementations wrap a specific library (``face_recognition``,
    ``DeepFace``, etc.) behind this uniform ``embed`` contract.
    The runner doesn't know or care which library produces the
    embedding — only that it gets a float vector of the documented
    dimensionality.
    """

    @abc.abstractmethod
    def embed(self, face_rgb: np.ndarray) -> list[float]:
        """Compute a face embedding from an RGB face crop.

        Parameters
        ----------
        face_rgb:
            An RGB ``uint8`` numpy array of shape ``(H, W, 3)``
            containing a tightly-cropped, aligned face.

        Returns
        -------
        list[float]
            The embedding vector.  Dimensionality depends on the
            backend (128 for dlib, 512 for ArcFace, etc.).

        Raises
        ------
        ValueError
            If no face is found in the crop, or the crop is invalid.
        """

    @property
    @abc.abstractmethod
    def embedding_dim(self) -> int:
        """Dimensionality of the embedding vector (e.g. 128 for dlib)."""

    @property
    @abc.abstractmethod
    def model_name(self) -> str:
        """Short identifier for the model (e.g. ``"dlib_resnet_v1"``)."""


# ---------------------------------------------------------------------------
# face_recognition (dlib) backend
# ---------------------------------------------------------------------------

class FaceRecognitionBackend(IdentityBackend):
    """Concrete backend wrapping ``face_recognition.face_encodings``.

    Uses the dlib ResNet model (128-d embeddings).  The library ships
    pre-trained weights inside the ``dlib`` wheel, so no external
    download is required on platforms where ``dlib`` installs.

    On Windows, ``dlib`` / ``face_recognition`` require MSVC Build
    Tools.  Tests that exercise this backend use
    ``pytest.importorskip("face_recognition")``.
    """

    def __init__(self) -> None:
        # Defer the import so the module is importable everywhere;
        # only construction of this backend class requires the lib.
        import face_recognition as _fr  # noqa: F401 — used below
        self._fr = _fr

    def embed(self, face_rgb: np.ndarray) -> list[float]:
        """Compute a 128-d dlib ResNet embedding.

        ``face_recognition.face_encodings`` expects an RGB uint8 array.
        It internally detects faces; if the crop is tight enough,
        exactly one face encoding is returned.

        Raises
        ------
        ValueError
            If ``face_recognition`` finds no face in the crop.
        """

        if face_rgb.ndim != 3 or face_rgb.shape[2] != 3:
            raise ValueError(
                f"Expected an RGB (H, W, 3) array; got shape {face_rgb.shape}."
            )

        encodings = self._fr.face_encodings(face_rgb)
        if not encodings:
            raise ValueError(
                "No face found in the provided crop; cannot compute embedding."
            )
        return encodings[0].tolist()

    @property
    def embedding_dim(self) -> int:
        return 128

    @property
    def model_name(self) -> str:
        return "dlib_resnet_v1"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class IdentityMatchRunner:
    """Stateless per-frame identity matcher.

    Accepts an ``IdentityBackend`` at construction time and uses it
    to embed face crops.  The ``run`` method compares the resulting
    embedding against an enrollment vector via cosine similarity.

    The match/mismatch threshold is **not hardcoded** — it's passed
    as an argument to ``run`` and should come from
    ``PolicyConfig.identity_similarity_threshold`` at the call site.
    """

    def __init__(self, backend: IdentityBackend) -> None:
        self._backend = backend

    @property
    def backend(self) -> IdentityBackend:
        """The embedding backend in use."""
        return self._backend

    def run(
        self,
        face_rgb: np.ndarray,
        enrollment_embedding: list[float],
        *,
        similarity_threshold: float,
    ) -> IdentityMatchResult:
        """Compare a face crop against the enrollment embedding.

        Parameters
        ----------
        face_rgb:
            RGB ``uint8`` numpy array of the cropped face region.
        enrollment_embedding:
            The stored embedding from ``EnrollmentReference.embedding_vector``.
        similarity_threshold:
            The minimum cosine similarity for a positive match.
            Must come from ``PolicyConfig``, not hardcoded.

        Returns
        -------
        IdentityMatchResult
            ``event_type`` is ``"identity_match"`` if similarity >=
            threshold, else ``"identity_mismatch"``.
        """

        if not (0.0 <= similarity_threshold <= 1.0):
            raise ValueError(
                f"similarity_threshold must be in [0, 1]; got {similarity_threshold}."
            )

        current_embedding = self._backend.embed(face_rgb)
        similarity = compute_cosine_similarity(
            current_embedding, enrollment_embedding
        )

        if similarity >= similarity_threshold:
            event_type = EVENT_IDENTITY_MATCH
        else:
            event_type = EVENT_IDENTITY_MISMATCH

        # Single-frame point estimate — the fusion engine computes
        # the multi-frame confidence interval.
        confidence = ConfidenceInterval(
            lower=similarity,
            score=similarity,
            upper=similarity,
        )

        return IdentityMatchResult(
            modality=_MODALITY,
            event_type=event_type,
            confidence=confidence,
            bounding_boxes=[],
            raw_value={
                "similarity": similarity,
                "threshold": similarity_threshold,
                "embedding_dim": self._backend.embedding_dim,
                "model_name": self._backend.model_name,
            },
            similarity=similarity,
        )
