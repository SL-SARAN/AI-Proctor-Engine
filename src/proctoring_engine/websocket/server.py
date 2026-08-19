"""Server-side WebSocket message types and delivery service.

This module defines the server → client message envelope types and
the :class:`DeliveryService` that manages the kill-switch delivery
lifecycle, per-frame acknowledgements, and telemetry event buffering
before hand-off to the preprocessing layer.

Server → client envelope shape (``docs/02-ingestion-layer-design.md`` §4)::

    {
        "type": "ack | kill_switch | policy_update",
        "payload": { ... }
    }

**Design choices:**

- The kill-switch message carries the ``flag_id`` so the client can
  echo it in the acknowledgement. The server matches the ack to
  the correct ``TerminationRecord`` via the delivery service
  state machine; the orchestration layer is the one that writes
  ``TerminationRecord.client_acknowledged_at`` (the actual column
  name on the ORM model — the docs/06 narrative uses
  ``kill_switch_ack_at`` as a conceptual shorthand).
- Acknowledgements are per-frame: every ingested envelope gets a
  monotonically-increasing ``seq`` back so the client can detect
  missed acks and re-send.  This is belt-and-suspenders alongside the
  TCP-level guarantees of WebSocket — it lets the client track
  application-level "was this frame processed" without relying on
  transport-level reliability alone.
- The :class:`TelemetryEventBuffer` is a bounded, thread-safe
  in-memory buffer.  It is the handoff point between the WebSocket
  handler (producer) and the preprocessing layer (consumer).  The
  buffer drops oldest entries on overflow rather than blocking the
  connection handler — a dropped frame is less harmful than a
  stalled WebSocket.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from proctoring_engine.websocket.client import ClientMessage


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error surface
# ---------------------------------------------------------------------------

class KillSwitchDeliverError(Exception):
    """Raised when a kill-switch delivery fails."""


# ---------------------------------------------------------------------------
# Server → client message types
# ---------------------------------------------------------------------------

class ServerMessageType(str, enum.Enum):
    """Discriminator values for server → client messages."""

    ACK = "ack"
    KILL_SWITCH = "kill_switch"
    POLICY_UPDATE = "policy_update"


# ---------------------------------------------------------------------------
# Server → client payload / envelope models
# ---------------------------------------------------------------------------

class SessionAcknowledgePayload(BaseModel):
    """Per-frame acknowledgement payload."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(
        ...,
        ge=0,
        description="Monotonically increasing sequence number for this session.",
    )
    received_at: datetime = Field(
        ...,
        description="Server-side wall-clock timestamp when the frame was processed.",
    )


class SessionAcknowledgeDeliver(BaseModel):
    """Server → client: acknowledgement of a received client message."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[ServerMessageType.ACK] = ServerMessageType.ACK
    payload: SessionAcknowledgePayload

    def to_json_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict for ``websocket.send_json``."""
        return self.model_dump(mode="json")


class KillSwitchPayload(BaseModel):
    """Kill-switch delivery payload.

    The ``reason`` field is a closed enumeration of the conditions
    that can trigger a kill-switch.  ``flag_id`` ties the delivery
    back to the ``Flag`` row for audit.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        ...,
        description=(
            "Termination reason code "
            "(second_person_detected | gaze_frequency_exceeded | "
            "accumulated_score_exceeded)."
        ),
    )
    flag_id: str = Field(
        ...,
        min_length=1,
        description="UUID of the Flag row that triggered the kill-switch.",
    )


class KillSwitchDeliver(BaseModel):
    """Server → client: kill-switch instruction."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[ServerMessageType.KILL_SWITCH] = ServerMessageType.KILL_SWITCH
    payload: KillSwitchPayload

    def to_json_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict for ``websocket.send_json``."""
        return self.model_dump(mode="json")


class PolicyUpdatePayload(BaseModel):
    """Mid-session policy update payload.

    Sent when an administrator changes a policy parameter that affects
    the running session.  The exam client adjusts its capture
    parameters (e.g. frame rate, audio sample rate) without a
    reconnect.
    """

    model_config = ConfigDict(extra="forbid")

    updates: dict[str, Any] = Field(
        default_factory=dict,
        description="Key-value pairs of updated policy parameters.",
    )


class PolicyUpdateDeliver(BaseModel):
    """Server → client: policy-parameter update."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[ServerMessageType.POLICY_UPDATE] = ServerMessageType.POLICY_UPDATE
    payload: PolicyUpdatePayload

    def to_json_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict for ``websocket.send_json``."""
        return self.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Telemetry event buffer (WebSocket handler → preprocessing layer)
# ---------------------------------------------------------------------------

@dataclass
class BufferedEvent:
    """A parsed client message with server-side arrival metadata.

    This is the unit the preprocessing layer consumes.

    ``synthetic_id`` is a per-event synthetic UUID minted by the
    FrameDispatcher when the event is consumed.  The persistence
    layer (turn 9b) uses this as the primary key for the
    corresponding ``TelemetryEvent`` row, so the dispatcher's flag
    decisions and the DB rows share the same identifier.  ``None``
    before the dispatcher has assigned one (e.g. the message is
    still in the buffer).
    """

    message: ClientMessage
    received_at: datetime
    seq: int
    synthetic_id: "uuid.UUID | None" = None


class TelemetryEventBuffer:
    """Bounded, thread-safe FIFO buffer for ingested telemetry events.

    The WebSocket handler writes parsed :class:`ClientMessage` objects
    into the buffer via :meth:`push`.  The preprocessing layer drains
    them via :meth:`drain`.  Overflow drops the oldest events rather
    than blocking the WebSocket handler.

    Parameters
    ----------
    maxlen : int
        Maximum number of events the buffer holds.  When full, the
        oldest event is silently dropped on the next :meth:`push`.
    """

    def __init__(self, maxlen: int = 4096) -> None:
        if maxlen <= 0:
            raise ValueError("Buffer maxlen must be positive.")
        self._buf: deque[BufferedEvent] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._seq = 0
        self._dropped = 0

    @property
    def dropped_count(self) -> int:
        """Number of events dropped due to overflow since creation."""
        with self._lock:
            return self._dropped

    def push(self, message: ClientMessage) -> BufferedEvent:
        """Add a parsed message to the buffer.

        Returns the :class:`BufferedEvent` wrapping the message
        (including the monotonic ``seq`` number).  If the buffer is
        full, the oldest event is silently dropped.
        """

        now = datetime.now(timezone.utc)
        with self._lock:
            was_full = len(self._buf) == self._buf.maxlen
            event = BufferedEvent(message=message, received_at=now, seq=self._seq)
            self._buf.append(event)
            self._seq += 1
            if was_full:
                self._dropped += 1
                logger.warning(
                    "Telemetry event buffer overflow; oldest event dropped "
                    "(total dropped: %d).",
                    self._dropped,
                )
        return event

    def drain(self) -> list[BufferedEvent]:
        """Remove and return all buffered events in FIFO order."""

        with self._lock:
            events = list(self._buf)
            self._buf.clear()
        return events

    def __len__(self) -> int:
        with self._lock:
            return len(self._buf)


# ---------------------------------------------------------------------------
# Delivery service (kill-switch + ack lifecycle)
# ---------------------------------------------------------------------------

@dataclass
class _PendingKillSwitch:
    """Internal record of a kill-switch that has been sent but not acked."""

    flag_id: str
    sent_at: float  # time.monotonic()
    retries: int = 0


class DeliveryService:
    """Manages kill-switch delivery and per-frame ack generation.

    One instance per active WebSocket connection.  The handler calls
    :meth:`next_ack` after each successfully ingested message, and
    :meth:`prepare_kill_switch` / :meth:`record_kill_switch_ack`
    around the termination path.

    Parameters
    ----------
    ack_grace_seconds : float
        Maximum time (seconds) after sending a kill-switch before
        the server considers it un-acked and eligible for retry.
        Default 30 s (per the §2 heartbeat grace window).
    max_retries : int
        Maximum number of kill-switch re-delivery attempts before
        the delivery is marked FAILED.
    """

    def __init__(
        self,
        *,
        ack_grace_seconds: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._seq = 0
        self._ack_grace = ack_grace_seconds
        self._max_retries = max_retries
        self._pending_kill: _PendingKillSwitch | None = None
        self._kill_switch_acked = False

    @property
    def current_seq(self) -> int:
        """The sequence number that will be used for the *next* ack."""
        return self._seq

    @property
    def has_pending_kill_switch(self) -> bool:
        """Whether a kill-switch has been sent but not yet acked."""
        return self._pending_kill is not None and not self._kill_switch_acked

    @property
    def kill_switch_acked(self) -> bool:
        """Whether the last kill-switch was acknowledged by the client."""
        return self._kill_switch_acked

    def next_ack(self) -> SessionAcknowledgeDeliver:
        """Generate the next per-frame acknowledgement.

        Each call increments the monotonic sequence counter.
        """

        now = datetime.now(timezone.utc)
        ack = SessionAcknowledgeDeliver(
            payload=SessionAcknowledgePayload(seq=self._seq, received_at=now),
        )
        self._seq += 1
        return ack

    def prepare_kill_switch(
        self,
        flag_id: str,
        reason: str,
    ) -> KillSwitchDeliver:
        """Build a kill-switch delivery message and register it as pending.

        Raises :class:`KillSwitchDeliverError` if a kill-switch is
        already pending (only one can be in-flight at a time).
        """

        if self._pending_kill is not None and not self._kill_switch_acked:
            raise KillSwitchDeliverError(
                f"A kill-switch for flag '{self._pending_kill.flag_id}' is "
                "already pending; cannot queue a second one."
            )

        self._pending_kill = _PendingKillSwitch(
            flag_id=flag_id,
            sent_at=time.monotonic(),
        )
        self._kill_switch_acked = False

        return KillSwitchDeliver(
            payload=KillSwitchPayload(reason=reason, flag_id=flag_id),
        )

    def record_kill_switch_ack(self, flag_id: str) -> bool:
        """Record a kill-switch acknowledgement from the client.

        Returns ``True`` if the ack matched the pending kill-switch;
        ``False`` if there is no pending kill-switch or the ``flag_id``
        does not match (possible replay / stale ack).
        """

        if self._pending_kill is None:
            logger.warning(
                "Received kill-switch ack for flag '%s' but no kill-switch is pending.",
                flag_id,
            )
            return False

        if self._pending_kill.flag_id != flag_id:
            logger.warning(
                "Received kill-switch ack for flag '%s' but pending kill-switch "
                "is for flag '%s'; ignoring.",
                flag_id,
                self._pending_kill.flag_id,
            )
            return False

        self._kill_switch_acked = True
        return True

    def should_retry_kill_switch(self) -> KillSwitchDeliver | None:
        """Check whether the pending kill-switch should be re-sent.

        Returns a re-delivery message if the grace period has elapsed
        and the retry budget has not been exhausted, otherwise ``None``.
        """

        if self._pending_kill is None or self._kill_switch_acked:
            return None

        elapsed = time.monotonic() - self._pending_kill.sent_at
        if elapsed < self._ack_grace:
            return None

        if self._pending_kill.retries >= self._max_retries:
            logger.error(
                "Kill-switch for flag '%s' not acked after %d retries; giving up.",
                self._pending_kill.flag_id,
                self._max_retries,
            )
            return None

        self._pending_kill.retries += 1
        self._pending_kill.sent_at = time.monotonic()
        logger.warning(
            "Retrying kill-switch for flag '%s' (attempt %d/%d).",
            self._pending_kill.flag_id,
            self._pending_kill.retries,
            self._max_retries,
        )

        return KillSwitchDeliver(
            payload=KillSwitchPayload(
                reason="retry",
                flag_id=self._pending_kill.flag_id,
            ),
        )
