"""Unmocked integration test for the real uniface package and UnifaceBackend.

Directly verifies that the real uniface library exposes create_spoofer and
.predict(image, bbox), and that UnifaceBackend correctly interacts with the
real library without any mocks/patches.
"""

from __future__ import annotations

import os
import numpy as np
import pytest

from proctoring_engine.inference.liveness import (
    UnifaceBackend,
    LivenessRunner,
    EVENT_LIVENESS_REAL,
    EVENT_LIVENESS_SPOOF,
)


@pytest.mark.integration
def test_unmocked_uniface_spoofer_real_library_contract() -> None:
    """Genuinely unmocked test against the installed uniface package.

    Asserts:
    1. uniface.create_spoofer exists and returns a real MiniFASNet spoofer.
    2. spoofer.predict(frame_bgr, bbox_xyxy) returns SpoofingResult with is_real (bool)
       and confidence (float).
    3. UnifaceBackend(providers=['CPUExecutionProvider']) executes against the real
       model weights and returns a 2-tuple: tuple[bool, float].
    4. LivenessRunner wrapped around UnifaceBackend produces a valid LivenessResult
       with boolean is_real and statistical ConfidenceInterval.
    """
    uniface = pytest.importorskip("uniface")
    from uniface.constants import MiniFASNetWeights

    # 1. Direct uniface API call with zero mocks
    spoofer = uniface.create_spoofer(
        model_name=MiniFASNetWeights.V2,
        providers=["CPUExecutionProvider"],
    )
    assert spoofer is not None

    # Synthetic BGR heavy frame (480x640x3 uint8) + xyxy bounding box
    frame_bgr = np.zeros((480, 640, 3), dtype=np.uint8)
    bbox_xyxy = [100, 100, 300, 300]

    raw_result = spoofer.predict(frame_bgr, bbox_xyxy)
    assert hasattr(raw_result, "is_real"), "SpoofingResult must have is_real attribute"
    assert hasattr(raw_result, "confidence"), "SpoofingResult must have confidence attribute"
    assert isinstance(raw_result.is_real, bool)
    assert isinstance(raw_result.confidence, float)
    assert 0.0 <= raw_result.confidence <= 1.0

    # 2. UnifaceBackend execution with zero mocks
    backend = UnifaceBackend(providers=["CPUExecutionProvider"])
    assert backend.model_name == "MiniFASNetV2"

    is_real, confidence = backend.predict(frame_bgr, bbox_xyxy)
    assert isinstance(is_real, bool)
    assert isinstance(confidence, float)
    assert 0.0 <= confidence <= 1.0

    # 3. Full runner evaluation
    runner = LivenessRunner(backend)
    liveness_result = runner.run(frame_bgr, bbox_xyxy, confidence_threshold=0.5)
    assert isinstance(liveness_result.is_real, bool)
    assert liveness_result.event_type in (EVENT_LIVENESS_REAL, EVENT_LIVENESS_SPOOF)
    assert liveness_result.confidence.score == confidence
    assert liveness_result.raw_value["model_name"] == "MiniFASNetV2"
