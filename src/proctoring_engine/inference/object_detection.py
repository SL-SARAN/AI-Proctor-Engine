"""Object detection inference module (YOLOv8, denylist strategy).

Wraps Ultralytics YOLOv8 pretrained on COCO.  The model weights
(``.pt`` file) must be pre-baked into the container image and their
path passed via the ``YOLO_WEIGHTS_PATH`` environment variable —
resolved 2026-07-24 per explicit user decision.

**Denylist strategy** (``docs/proctoring-engine-v1-spec.md`` §3.2):
the detector runs over the full COCO 80-class set but only emits
results for the four denylisted classes:

- ``cell phone`` (COCO class 67)
- ``laptop`` (COCO class 63)
- ``tv`` (COCO class 62) — proxy for second monitor
- ``book`` (COCO class 73) — conditional on exam's
  ``allowed_reference_materials`` setting; this module *always*
  emits the detection, and the fusion engine decides severity.

Non-denylist detections are discarded here, not persisted.

**Earbuds and smartwatches** are explicitly out of v1 scope — COCO
has no class for either, and fine-tuning was deferred.

Input:  a :class:`~proctoring_engine.preprocessing.frames.DecodedFrame`.
        The Ultralytics model assumes **BGR** input — it does *not*
        detect or convert channel order internally.  The
        preprocessing layer (``frames.py``) natively decodes into
        BGR, so the array is passed through unchanged.
Output: ``list[ObjectDetectionResult]`` — one per detected denylist
        object, or an empty list if nothing on the denylist was found.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Final

from proctoring_engine.inference._types import (
    BoundingBox,
    ConfidenceInterval,
    ObjectDetectionResult,
)
from proctoring_engine.preprocessing.frames import DecodedFrame


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

YOLO_WEIGHTS_PATH_ENV: Final[str] = "YOLO_WEIGHTS_PATH"
"""Environment variable pointing to the YOLOv8 ``.pt`` weights file."""

_MODALITY: Final[str] = "object"

# COCO class IDs for the four denylisted objects.
# Source: the COCO 80-class list shipped with Ultralytics.
# Verified against ``ultralytics.nn.autobackend`` default class names.
COCO_DENYLIST_IDS: Final[dict[int, str]] = {
    62: "tv",
    63: "laptop",
    67: "cell phone",
    73: "book",
}
"""Mapping from COCO class ID to the denylisted object name."""

DENYLIST_CLASSES: Final[frozenset[str]] = frozenset(COCO_DENYLIST_IDS.values())
"""The four v1-denylisted class names as a frozen set."""


# ---------------------------------------------------------------------------
# Denylist filter (pure function, unit-testable without YOLO)
# ---------------------------------------------------------------------------

def filter_denylist_detections(
    boxes: Any,
    class_names: dict[int, str],
    denylist: frozenset[str],
) -> list[dict[str, Any]]:
    """Filter YOLO prediction boxes to only denylist classes.

    Parameters
    ----------
    boxes:
        The ``results[0].boxes`` object from a YOLO predict call.
        Expected attributes: ``.xyxyn`` (normalised coordinates),
        ``.conf`` (confidence tensor), ``.cls`` (class ID tensor).
    class_names:
        The model's ``names`` dict (class-id → class-name).
    denylist:
        Set of class names to keep.

    Returns
    -------
    list[dict[str, Any]]
        Each dict has ``class_name``, ``confidence``, ``bbox``
        (normalised ``[x, y, w, h]``).
    """

    results: list[dict[str, Any]] = []
    if boxes is None:
        return results

    for i in range(len(boxes.cls)):
        cls_id = int(boxes.cls[i].item())
        cls_name = class_names.get(cls_id, "")
        if cls_name not in denylist:
            continue

        conf = float(boxes.conf[i].item())

        # xyxyn is normalised [x1, y1, x2, y2]. Let it safely clamp
        # to [0, 1] before computing width/height so x + w never
        # exceeds 1.0 even on edge-case predictions that spill
        # out of frame.
        xyxyn = boxes.xyxyn[i].tolist()
        x1 = max(0.0, min(1.0, float(xyxyn[0])))
        y1 = max(0.0, min(1.0, float(xyxyn[1])))
        x2 = max(0.0, min(1.0, float(xyxyn[2])))
        y2 = max(0.0, min(1.0, float(xyxyn[3])))
        bbox = {
            "x": x1,
            "y": y1,
            "w": x2 - x1,
            "h": y2 - y1,
        }
        results.append({
            "class_name": cls_name,
            "confidence": conf,
            "bbox": bbox,
        })

    return results


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

class ObjectDetectorRunner:
    """Stateless per-frame object detector (YOLOv8, denylist filter).

    Parameters
    ----------
    weights_path:
        Filesystem path to the YOLOv8 ``.pt`` weights.  If ``None``,
        reads ``YOLO_WEIGHTS_PATH`` from the environment.
    confidence_threshold:
        Minimum confidence for a detection to be included.
        Default 0.4 — a reasonable starting point for the COCO
        pretrained model.  This is a constructor argument, not
        hardcoded.

    Raises
    ------
    EnvironmentError
        If no weights path is available.
    FileNotFoundError
        If the resolved path does not exist.
    """

    def __init__(
        self,
        *,
        weights_path: str | None = None,
        confidence_threshold: float = 0.4,
    ) -> None:
        if weights_path is None:
            weights_path = os.environ.get(YOLO_WEIGHTS_PATH_ENV)
        if weights_path is None:
            raise EnvironmentError(
                f"The {YOLO_WEIGHTS_PATH_ENV} environment variable "
                f"is not set and no weights_path was provided."
            )
        if not os.path.isfile(weights_path):
            raise FileNotFoundError(
                f"YOLO weights not found at '{weights_path}'."
            )

        self._confidence_threshold = confidence_threshold

        from ultralytics import YOLO
        self._model = YOLO(weights_path)

    def run(self, frame: DecodedFrame) -> list[ObjectDetectionResult]:
        """Detect denylist objects in a single frame.

        Parameters
        ----------
        frame:
            A :class:`DecodedFrame`. Must be in **BGR** channel order
            (the native layout produced by the preprocessing layer)
            because the Ultralytics model assumes BGR input and does
            not convert it.

        Returns
        -------
        list[ObjectDetectionResult]
            One result per detected denylist object.  Empty list if
            no denylist objects are found.
        """

        pred = self._model.predict(
            frame.array,
            conf=self._confidence_threshold,
            verbose=False,
        )
        if not pred or pred[0].boxes is None:
            return []

        filtered = filter_denylist_detections(
            pred[0].boxes,
            self._model.names,
            DENYLIST_CLASSES,
        )

        results: list[ObjectDetectionResult] = []
        for det in filtered:
            bbox = BoundingBox(
                x=det["bbox"]["x"],
                y=det["bbox"]["y"],
                w=det["bbox"]["w"],
                h=det["bbox"]["h"],
            )
            conf = det["confidence"]
            confidence = ConfidenceInterval(
                lower=conf, score=conf, upper=conf,
            )
            results.append(
                ObjectDetectionResult(
                    modality=_MODALITY,
                    event_type=det["class_name"],
                    confidence=confidence,
                    bounding_boxes=[bbox],
                    raw_value={
                        "detected_class": det["class_name"],
                        "yolo_confidence": conf,
                    },
                    detected_class=det["class_name"],
                )
            )

        return results
