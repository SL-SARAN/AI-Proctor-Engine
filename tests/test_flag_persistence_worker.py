"""Tests for the FlagPersistenceWorker (turn 9b).

The worker runs in a background thread, drains the FrameDispatcher's
queue, inserts TelemetryEvent rows for the contributing events,
and then calls _flag_persistence.persist_flag_decision to write the
immutable Flag rows.
"""

from __future__ import annotations

import queue
import time
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
from sqlalchemy.exc import IntegrityError

from proctoring_engine.fusion._types import FlagDecision
from proctoring_engine.inference._types import ConfidenceInterval
from proctoring_engine.models import (
    ExamSession,
    Flag,
    TelemetryEvent,
    TelemetryModality,
)
from proctoring_engine.orchestration._flag_persistence import (
    FlagPersistenceError,
)
from proctoring_engine.orchestration._flag_persistence_worker import (
    FlagPersistenceWorker,
    _build_telemetry_event_row,
)
from proctoring_engine.orchestration._frame_dispatcher import PersistedFlag
from proctoring_engine.websocket.client import (
    TelemetryBrowserEvent,
    TelemetryHeavyFrame,
    TelemetryLight,
)
from proctoring_engine.websocket.server import BufferedEvent


def _make_buffered(synthetic_id: uuid.UUID) -> BufferedEvent:
    msg = TelemetryLight(
        session_id=str(synthetic_id),
        captured_at=datetime.now(timezone.utc),
        payload={"face_count": 0, "confidence": 1.0, "bbox": None},
    )
    ev = BufferedEvent(message=msg, received_at=datetime.now(timezone.utc), seq=0)
    ev.synthetic_id = synthetic_id
    return ev


def _make_persisted_flag(
    *synthetic_ids: uuid.UUID,
) -> PersistedFlag:
    events = tuple(_make_buffered(eid) for eid in synthetic_ids)
    decision = FlagDecision(
        rule_code="test_rule",
        severity="medium",
        confidence=ConfidenceInterval(0.5, 0.5, 0.5),
        contributing_event_ids=synthetic_ids,
    )
    return PersistedFlag(decision=decision, contributing_events=events)


class TestFlagPersistenceWorker:
    """Verifies the queue draining and DB interaction."""

    def test_worker_lifecycle(self) -> None:
        dispatcher = MagicMock()
        worker = FlagPersistenceWorker(dispatcher=dispatcher, get_db=MagicMock())
        assert worker._thread is None
        worker.start()
        assert worker._thread is not None
        assert worker._thread.is_alive()
        worker.stop()
        assert worker._thread is None

    @patch("proctoring_engine.orchestration._flag_persistence_worker.persist_flag_decision")
    def test_successful_persistence(self, mock_persist: MagicMock) -> None:
        """A normal queue item results in TelemetryEvent inserts and a
        persist_flag_decision call."""
        dispatcher = MagicMock()
        dispatcher._config.context.exam_session_id = uuid.uuid4()
        dispatcher.flag_decisions = queue.Queue()

        db = MagicMock()
        session = MagicMock(spec=ExamSession, id=uuid.uuid4())
        db.get.return_value = session

        worker = FlagPersistenceWorker(dispatcher=dispatcher, get_db=lambda: db)

        # Enqueue item
        eid = uuid.uuid4()
        item = _make_persisted_flag(eid)
        dispatcher.flag_decisions.put(item)

        worker.start()
        try:
            # Wait for processing
            start_time = time.monotonic()
            while worker.persisted_count < 1 and time.monotonic() - start_time < 2.0:
                time.sleep(0.01)
        finally:
            worker.stop()

        assert worker.persisted_count == 1
        assert worker.error_count == 0

        # Mocks verification
        assert db.add.call_count == 1
        row = db.add.call_args[0][0]
        assert isinstance(row, TelemetryEvent)
        assert row.id == eid

        mock_persist.assert_called_once_with(
            db, item.decision, exam_session=session
        )
        db.commit.assert_called_once()
        db.close.assert_called_once()

    @patch("proctoring_engine.orchestration._flag_persistence_worker.persist_flag_decision")
    def test_session_missing_drops_decision(self, mock_persist: MagicMock) -> None:
        """If the ExamSession row is gone, the worker drops the item
        and does not raise."""
        dispatcher = MagicMock()
        dispatcher._config.context.exam_session_id = uuid.uuid4()
        dispatcher.flag_decisions = queue.Queue()

        db = MagicMock()
        db.get.return_value = None  # Missing session

        worker = FlagPersistenceWorker(dispatcher=dispatcher, get_db=lambda: db)
        dispatcher.flag_decisions.put(_make_persisted_flag(uuid.uuid4()))

        worker.start()
        try:
            time.sleep(0.2)
        finally:
            worker.stop()

        # Did not fail loop, but skipped processing (item was drained)
        assert worker.persisted_count == 1
        assert worker.error_count == 0
        mock_persist.assert_not_called()
        db.add.assert_not_called()
        db.close.assert_called()

    @patch("proctoring_engine.orchestration._flag_persistence_worker.persist_flag_decision")
    def test_duplicate_telemetry_event_ignored(self, mock_persist: MagicMock) -> None:
        """If a TelemetryEvent insert hits an IntegrityError (already
        exists from a prior run), the worker catches it, rolls back
        the insert, and continues to persist the Flag."""
        dispatcher = MagicMock()
        dispatcher._config.context.exam_session_id = uuid.uuid4()
        dispatcher.flag_decisions = queue.Queue()

        db = MagicMock()
        db.get.return_value = MagicMock(spec=ExamSession, id=uuid.uuid4())
        db.flush.side_effect = [IntegrityError("stmt", "params", "orig"), None]
        # Make begin_nested a viable context manager
        db.begin_nested.return_value.__enter__.return_value = MagicMock()
        db.begin_nested.return_value.__exit__.return_value = False

        worker = FlagPersistenceWorker(dispatcher=dispatcher, get_db=lambda: db)
        dispatcher.flag_decisions.put(_make_persisted_flag(uuid.uuid4()))

        worker.start()
        try:
            start_time = time.monotonic()
            while worker.persisted_count < 1 and time.monotonic() - start_time < 2.0:
                time.sleep(0.01)
        finally:
            worker.stop()

        # The mock doesn't implement fully the context manager of begin_nested,
        # but db.commit is still called on the outer transaction.
        assert worker.persisted_count == 1
        assert worker.error_count == 0
        mock_persist.assert_called_once()
        db.commit.assert_called_once()

    def test_persist_failure_increments_error_count(self) -> None:
        """If flag persistence raises, the error is caught, the DB
        rolls back, and the worker loop continues."""
        dispatcher = MagicMock()
        dispatcher._config.context.exam_session_id = uuid.uuid4()
        dispatcher.flag_decisions = queue.Queue()

        # A DB that blows up on commit
        db = MagicMock()
        db.get.return_value = MagicMock(spec=ExamSession, id=uuid.uuid4())
        db.commit.side_effect = Exception("Boom")

        worker = FlagPersistenceWorker(dispatcher=dispatcher, get_db=lambda: db)
        dispatcher.flag_decisions.put(_make_persisted_flag(uuid.uuid4()))

        worker.start()
        try:
            start_time = time.monotonic()
            while worker.error_count < 1 and time.monotonic() - start_time < 2.0:
                time.sleep(0.01)
        finally:
            worker.stop()

        assert worker.error_count == 1
        assert worker.persisted_count == 0
        db.rollback.assert_called_once()
        db.close.assert_called_once()


class TestBuildTelemetryEventRow:
    """Verify BufferedEvent → TelemetryEvent payload mapping."""

    def test_build_light(self) -> None:
        eid = uuid.uuid4()
        ev = _make_buffered(eid)
        row = _build_telemetry_event_row(ev)
        assert row.id == eid
        assert row.modality == TelemetryModality.FACE
        assert row.event_type == "no_face"
        assert row.confidence == 1.0

    def test_build_heavy(self) -> None:
        eid = uuid.uuid4()
        msg = TelemetryHeavyFrame(
            session_id=str(eid),
            captured_at=datetime.now(timezone.utc),
            payload={
                "frame": "img",
                "resolution": [640, 480],
                "encoding": "jpeg",
            },
        )
        ev = BufferedEvent(message=msg, received_at=datetime.now(timezone.utc), seq=0)
        ev.synthetic_id = eid
        row = _build_telemetry_event_row(ev)
        assert row.modality == TelemetryModality.SYSTEM
        assert row.event_type == "heavy_frame_received"
        assert row.raw_value["resolution"] == [640, 480]

    def test_build_browser(self) -> None:
        eid = uuid.uuid4()
        msg = TelemetryBrowserEvent(
            session_id=str(eid),
            captured_at=datetime.now(timezone.utc),
            payload={"event_type": "blur", "detail": {"tag": "123"}},
        )
        ev = BufferedEvent(message=msg, received_at=datetime.now(timezone.utc), seq=0)
        ev.synthetic_id = eid
        row = _build_telemetry_event_row(ev)
        assert row.modality == TelemetryModality.BROWSER
        assert row.event_type == "blur"
        assert row.raw_value["detail"] == {"tag": "123"}
