"""Integration tests for the full FrameDispatcher → Flag persistence pipeline.

Exercises the end-to-end flow described in turn 9b:
1. FrameDispatcher receives a TelemetryHeavyFrame with a loud object (e.g. cell phone).
2. ModalityScheduler triggers ObjectDetectorRunner.
3. Aggregator emits a FlagDecision to ``flag_decisions``.
4. FlagPersistenceWorker drains the queue, inserts a TelemetryEvent,
   and writes the Flag row to the database.
"""

from __future__ import annotations

import base64
import time
import uuid
from datetime import datetime, timezone

import numpy as np

from proctoring_engine.models import (
    ExamSession,
    Flag,
    TelemetryEvent,
)
from proctoring_engine.orchestration._frame_dispatcher import (
    FrameDispatcher,
    FrameDispatcherConfig,
)
from proctoring_engine.orchestration._flag_persistence_worker import (
    FlagPersistenceWorker,
)
from proctoring_engine.websocket.client import TelemetryHeavyFrame
from proctoring_engine.websocket.server import TelemetryEventBuffer

from tests.test_fusion import _default_policy


def _make_telemetry_heavy() -> TelemetryHeavyFrame:
    """Build a tiny 32x32 BGR JPEG (encoded as base64)."""
    arr = np.zeros((32, 32, 3), dtype=np.uint8)
    arr[:, :, 2] = 255
    import cv2
    _, buf = cv2.imencode(".jpg", arr)
    encoded = base64.b64encode(buf.tobytes()).decode("ascii")
    return TelemetryHeavyFrame(
        session_id=str(uuid.uuid4()),
        captured_at=datetime.now(timezone.utc),
        payload={
            "frame": encoded,
            "resolution": [32, 32],
            "encoding": "jpeg",
        },
    )


def test_end_to_end_dispatcher_persistence(db_session) -> None:
    """End-to-end wiring test: heavy frame to Flag row."""
    # 1. Setup the DB constraints (requires real rows)
    from proctoring_engine.models import Participant, PolicyConfig
    from sqlalchemy.orm import Session

    participant = Participant(
        lti_issuer="https://lms.example.edu",
        lms_user_reference=f"student-{uuid.uuid4()}",
    )
    policy = PolicyConfig(name=f"policy-{uuid.uuid4()}")
    db_session.add_all([participant, policy])
    db_session.flush()

    exam_session = ExamSession(
        participant_id=participant.id,
        policy_config_id=policy.id,
        lti_issuer=participant.lti_issuer,
        lti_context_id="course-101",
        exam_reference="exam-202",
        attempt_reference=f"attempt-{uuid.uuid4()}",
    )
    db_session.add(exam_session)
    db_session.commit()

    # 2. Setup Dispatcher and Worker
    from proctoring_engine.fusion.aggregator import SessionContext

    ctx = SessionContext(
        exam_session_id=exam_session.id,
        participant_id=participant.id,
        exam_reference="exam-202",
        policy_config_id=policy.id,
    )
    config = FrameDispatcherConfig(
        policy_snapshot=_default_policy(),
        context=ctx,
        object_detection_period=1,
    )
    buf = TelemetryEventBuffer(maxlen=128)
    dispatcher = FrameDispatcher(config=config, event_buffer=buf)

    # We must patch the detector to return a denylist object so it raises a flag
    from unittest.mock import patch, MagicMock
    from proctoring_engine.inference._types import ObjectDetectionResult, ConfidenceInterval, BoundingBox

    mock_det_result = ObjectDetectionResult(
        modality="object",
        event_type="cell phone",
        confidence=ConfidenceInterval(0.95, 0.95, 0.95),
        bounding_boxes=[BoundingBox(0.1, 0.1, 0.5, 0.5)],
        raw_value={},
        detected_class="cell phone",
    )

    with patch.object(
        FrameDispatcher,
        "_ensure_object_detector",
        return_value=MagicMock(run=MagicMock(return_value=[mock_det_result])),
    ), patch.object(
        FrameDispatcher,
        "_ensure_face_landmarker",
        return_value=MagicMock(run=MagicMock(return_value=None)),
    ):
        worker = FlagPersistenceWorker(
            dispatcher=dispatcher,
            get_db=lambda: db_session, # Use the test DB fixture
        )

        # Run synchronously to avoid threaded SQLite isolation issues in the test
        buf.push(_make_telemetry_heavy())

        # Pull the item manually to process
        batch = buf.drain()
        for event in batch:
            dispatcher._dispatch_event(event)

        # Let the worker process all queue items
        item = dispatcher.flag_decisions.get(timeout=1.0)
        worker._persist_one(item)

    # 5. Verify the full pipeline wrote the DB row
    assert dispatcher.error_count == 0
    assert worker.error_count == 0

    flags = db_session.query(Flag).filter_by(exam_session_id=exam_session.id).all()
    assert len(flags) == 1
    flag = flags[0]
    assert flag.rule_code == "object_detected"
    assert flag.severity == "medium"
    assert flag.detail["detected_class"] == "cell phone"

    # Verify TelemetryEvent is linked correctly
    assert len(flag.telemetry_links) == 1
    telemetry = flag.telemetry_links[0].telemetry_event
    assert telemetry.modality.value == "system"
    assert telemetry.event_type == "heavy_frame_received"
    assert telemetry.raw_value["encoding"] == "jpeg"
