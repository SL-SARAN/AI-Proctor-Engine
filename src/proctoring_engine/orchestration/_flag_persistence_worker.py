"""FlagPersistenceWorker — drains the FrameDispatcher's
``flag_decisions`` queue and writes ``Flag`` rows to the database.

**Lifecycle:** one worker per exam session (or per process — both
work, the queue is thread-safe). Constructed alongside the
``FrameDispatcher``; runs in a background thread until the session
ends.

**Per-decision lifecycle:**

1. Drain a ``PersistedFlag`` from the queue.
2. Open a fresh DB session (``get_db()``).
3. Look up the ``ExamSession`` row by id; raise if missing.
4. Insert a ``TelemetryEvent`` row for every ``BufferedEvent`` in
   ``PersistedFlag.contributing_events`` whose ID isn't already in
   the DB.  The dispatcher's synthetic UUIDs become the row IDs —
   the persistence layer is the single owner of those IDs once the
   events are written, and the ``Flag.contributing_event_ids`` row
   uses the same IDs.
5. Call ``_flag_persistence.persist_flag_decision(db, decision, exam_session)``
   to insert the immutable ``Flag`` row + ``FlagTelemetryEvent`` join
   rows + (if any) accumulated-score delta.

**Idempotency:** if the same ``FlagDecision.contributing_event_ids``
includes a ``TelemetryEvent`` that's already been inserted (e.g. the
worker retried after a crash), the existing row is left alone — the
INSERT raises an IntegrityError, the worker logs and skips the link.
The ``Flag`` row itself is still persisted.

**Why a separate worker thread:** the dispatcher's main loop is
optimised for streaming — drain buffer, dispatch, push to queue.
Persisting to a database round-trip is the slow part (a single
INSERT can take 5-50ms). A separate worker absorbs the latency so the
WS handler thread never blocks.
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from typing import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from proctoring_engine.fusion.aggregator import FlagDecision
from proctoring_engine.models import (
    ExamSession,
    Flag,
    TelemetryEvent,
    TelemetryModality,
    TerminationRecord,
)
from proctoring_engine.orchestration._flag_persistence import (
    FlagPersistenceError,
    persist_flag_decision,
)
from proctoring_engine.orchestration._frame_dispatcher import (
    PersistedFlag,
)
from proctoring_engine.websocket.client import (
    ClientMessage,
    TelemetryAudioChunk,
    TelemetryBrowserEvent,
    TelemetryHeavyFrame,
    TelemetryLight,
)
from proctoring_engine.websocket.server import BufferedEvent


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BufferedEvent → TelemetryEvent row payload mapping
# ---------------------------------------------------------------------------


def _build_telemetry_event_row(event: BufferedEvent) -> TelemetryEvent:
    """Build a ``TelemetryEvent`` ORM instance from a ``BufferedEvent``.

    The dispatcher's synthetic UUID (the key under which the
    BufferedEvent is stored) is the row's primary key — the
    persistence layer is the single owner of that key once the row
    is inserted, and downstream ``FlagTelemetryEvent`` joins refer
    to the same ID.
    """
    message = event.message
    if isinstance(message, TelemetryLight):
        return _build_from_telemetry_light(message, event)
    if isinstance(message, TelemetryHeavyFrame):
        return _build_from_telemetry_heavy(message, event)
    if isinstance(message, TelemetryAudioChunk):
        return _build_from_telemetry_audio(message, event)
    if isinstance(message, TelemetryBrowserEvent):
        return _build_from_telemetry_browser(message, event)
    raise ValueError(
        f"Cannot build TelemetryEvent from message of type "
        f"{type(message).__name__!r}"
    )


def _build_from_telemetry_light(
    message: TelemetryLight,
    event: BufferedEvent,
) -> TelemetryEvent:
    return TelemetryEvent(
        id=event.synthetic_id,
        exam_session_id=event.synthetic_id,  # overridden below by worker
        modality=TelemetryModality.FACE,
        event_type=(
            "second_person"
            if message.payload.face_count >= 2
            else ("no_face" if message.payload.face_count == 0 else "one_face")
        ),
        occurred_at=message.captured_at,
        received_at=event.received_at,
        confidence=float(message.payload.confidence),
        raw_value={"bbox": list(message.payload.bbox)} if message.payload.bbox is not None else {},
        bounding_boxes=(
            [list(message.payload.bbox)]
            if message.payload.bbox is not None
            else []
        ),
    )


def _build_from_telemetry_heavy(
    message: TelemetryHeavyFrame,
    event: BufferedEvent,
) -> TelemetryEvent:
    return TelemetryEvent(
        id=event.synthetic_id,
        exam_session_id=event.synthetic_id,
        modality=TelemetryModality.SYSTEM,
        event_type="heavy_frame_received",
        occurred_at=message.captured_at,
        received_at=event.received_at,
        confidence=1.0,
        raw_value={
            "resolution": list(message.payload.resolution),
            "encoding": message.payload.encoding,
        },
        bounding_boxes=[],
    )


def _build_from_telemetry_audio(
    message: TelemetryAudioChunk,
    event: BufferedEvent,
) -> TelemetryEvent:
    return TelemetryEvent(
        id=event.synthetic_id,
        exam_session_id=event.synthetic_id,
        modality=TelemetryModality.AUDIO,
        event_type="audio_chunk_received",
        occurred_at=message.captured_at,
        received_at=event.received_at,
        confidence=1.0,
        raw_value={
            "sample_rate_hz": int(message.payload.sample_rate_hz),
            "duration_ms": int(message.payload.duration_ms),
        },
        bounding_boxes=[],
    )


def _build_from_telemetry_browser(
    message: TelemetryBrowserEvent,
    event: BufferedEvent,
) -> TelemetryEvent:
    return TelemetryEvent(
        id=event.synthetic_id,
        exam_session_id=event.synthetic_id,
        modality=TelemetryModality.BROWSER,
        event_type=message.payload.event_type,
        occurred_at=message.captured_at,
        received_at=event.received_at,
        confidence=1.0,
        raw_value={"detail": dict(message.payload.detail)},
        bounding_boxes=[],
    )


# ---------------------------------------------------------------------------
# FlagPersistenceWorker
# ---------------------------------------------------------------------------


class FlagPersistenceWorker:
    """Drains the FrameDispatcher's ``flag_decisions`` queue and
    persists Flag rows + TelemetryEvent rows.

    **Thread model:** the worker runs in a daemon thread, polling
    ``dispatcher.flag_decisions``.  Each iteration:

    1. Pulls one ``PersistedFlag``.
    2. Opens a DB session via ``get_db()``.
    3. Looks up the ``ExamSession`` (raises if missing).
    4. Inserts ``TelemetryEvent`` rows for each contributing event.
    5. Calls ``persist_flag_decision`` to insert the ``Flag`` +
    ``FlagTelemetryEvent`` rows.
    6. Closes the session.

    **Backpressure:** if the worker falls behind the dispatcher, the
    queue grows.  ``TelemetryEventBuffer`` is bounded, but
    ``flag_decisions`` is unbounded by design — the production
    deployer should add monitoring.
    """

    def __init__(
        self,
        *,
        dispatcher: Any,  # FrameDispatcher (avoids circular import)
        get_db: Callable[[], Session],
        on_kill_switch: "Callable[[str, str], None] | None" = None,
    ) -> None:
        """Construct the persistence worker.

        Parameters
        ----------
        on_kill_switch:
            Optional callback invoked when a persisted Flag has
            ``triggered_termination=True``.  The callback receives
            ``(flag_id, reason)``.  It is intended to enqueue the
            kill-switch for delivery over the WebSocket (handled
            asynchronously by the WS message loop).  ``None`` means
            the worker runs but no kill-switch is fired (e.g. in
            headless test environments without a WS).
        """
        self._dispatcher = dispatcher
        self._get_db = get_db
        self._on_kill_switch = on_kill_switch
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._persisted_count = 0
        self._error_count = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="FlagPersistenceWorker",
            daemon=True,
        )
        self._thread.start()
        logger.info("FlagPersistenceWorker started.")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        logger.info(
            "FlagPersistenceWorker stopped. persisted=%d errors=%d",
            self._persisted_count,
            self._error_count,
        )

    @property
    def persisted_count(self) -> int:
        return self._persisted_count

    @property
    def error_count(self) -> int:
        return self._error_count

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                # Block briefly to avoid busy-waiting when the queue
                # is empty.  ``get(timeout=0.05)`` returns ``Empty``
                # after 50 ms; we then check the stop event.
                item = self._dispatcher.flag_decisions.get(timeout=0.05)
            except queue.Empty:
                continue

            try:
                self._persist_one(item)
                self._persisted_count += 1
            except Exception:
                logger.exception(
                    "FlagPersistenceWorker failed to persist a decision."
                )
                self._error_count += 1

    def _persist_one(self, item: PersistedFlag) -> None:
        """Insert TelemetryEvent rows + the Flag row for one decision."""
        decision = item.decision
        events = item.contributing_events

        db = self._get_db()
        try:
            exam_session = self._resolve_session(db)
            if exam_session is None:
                logger.error(
                    "FlagPersistenceWorker: exam session %s not found; "
                    "dropping decision %s",
                    self._dispatcher._config.context.exam_session_id,
                    decision.rule_code,
                )
                return

            # Insert TelemetryEvent rows.  We tolerate the case where
            # the row already exists (e.g. retried after a crash) —
            # the IntegrityError is silently logged and the Flag
            # persists without the link.
            for event in events:
                self._insert_telemetry_event(db, exam_session.id, event)

            # Persist the Flag itself.  This will also create the
            # FlagTelemetryEvent links using the IDs the dispatcher
            # placed in contributing_event_ids.
            flag = persist_flag_decision(
                db, decision, exam_session=exam_session
            )
            db.commit()
            db.refresh(flag)

            # If this flag triggers termination, build the
            # TerminationRecord and fire the kill-switch callback.
            # The callback is the WS-layer handler that delivers the
            # message to the client; we don't await here because
            # we're in a sync DB worker thread.
            if decision.triggered_termination:
                self._handle_kill_switch(db, flag, decision)
        except FlagPersistenceError as exc:
            db.rollback()
            logger.error("Flag persistence raised FlagPersistenceError: %s", exc)
            raise
        except IntegrityError as exc:
            db.rollback()
            logger.warning(
                "Flag insert hit an integrity error (likely a duplicate "
                "TelemetryEvent or a race); dropping the decision. %s",
                exc,
            )
            raise
        except Exception as exc:
            db.rollback()
            logger.exception("Flag persistence failed: %s", exc)
            raise
        finally:
            db.close()

    def _handle_kill_switch(
        self,
        db: Session,
        flag: Flag,
        decision: FlagDecision,
    ) -> None:
        """Build a TerminationRecord and fire the kill-switch callback.

        The TerminationRecord is a 1:1 row keyed by ``exam_session_id``;
        if one already exists (e.g. the dispatcher fires two
        triggered_termination flags back-to-back), we update the
        ``triggering_flag_id`` to point at the latest one rather than
        failing the duplicate insert.

        The kill-switch callback is invoked last, after the
        TerminationRecord is committed.  If the callback raises, we
        log and continue — the record is the durable evidence;
        callback failure is not a persistence failure.
        """
        # Build the TerminationRecord row.
        termination = (
            db.query(TerminationRecord)
            .filter(
                TerminationRecord.exam_session_id
                == self._dispatcher._config.context.exam_session_id
            )
            .one_or_none()
        )
        if termination is None:
            termination = TerminationRecord(
                exam_session_id=self._dispatcher._config.context.exam_session_id,
                triggering_flag_id=flag.id,
                reason=decision.rule_code,
            )
            db.add(termination)
        else:
            # Update the existing record (append-only flag_id means we
            # don't replace — but the TerminationRecord has no
            # immutability trigger, so updating the trigger is allowed).
            termination.triggering_flag_id = flag.id
            termination.reason = decision.rule_code
        db.commit()
        db.refresh(termination)

        # Fire the kill-switch callback (which sends the WS message).
        # The flag_id is a string in the DeliveryService contract.
        if self._on_kill_switch is not None:
            try:
                self._on_kill_switch(str(flag.id), decision.rule_code)
            except Exception:
                logger.exception(
                    "Kill-switch callback raised; TerminationRecord row "
                    "was committed but the WS message was not delivered."
                )

    def _resolve_session(self, db: Session) -> ExamSession | None:
        """Look up the ExamSession by id; return None if missing."""
        return db.get(
            ExamSession, self._dispatcher._config.context.exam_session_id
        )

    def _insert_telemetry_event(
        self,
        db: Session,
        session_id: uuid.UUID,
        event: BufferedEvent,
    ) -> None:
        """Insert a single TelemetryEvent row.

        The row's id is the dispatcher's synthetic UUID (kept on
        ``BufferedEvent.synthetic_id`` for exactly this purpose).
        The ``exam_session_id`` is set to the actual session — the
        synthetic id is for row identity, not for FK routing.
        """
        # SQLAlchemy connection block: we must execute the insert
        # inside an active transaction. By using a SAVEPOINT, we
        # ensure that an IntegrityError doesn't poison the outer
        # transaction block, allowing persist_flag_decision to still
        # succeed and commit if a single event ID happens to be
        # duplicated.
        with db.begin_nested():
            row = _build_telemetry_event_row(event)
            row.exam_session_id = session_id
            db.add(row)
            try:
                db.flush()
            except IntegrityError:
                # Likely the event was already persisted on a previous
                # attempt.  The nested transaction gracefully rolls back,
                # the outer transaction remains valid.
                logger.debug(
                    "TelemetryEvent id %s already exists; skipping link.",
                    event.synthetic_id,
                )
                db.expunge(row)
