"""Client-side WebSocket envelope types for the exam proctoring protocol.

This module defines the Pydantic v2 models for every message the exam
client can send to the server over the authenticated WebSocket
connection.  The message envelope is the contract between the browser
capture layer (``docs/02-ingestion-layer-design.md`` §3) and the
server-side ingestion handler.

Envelope shape (all client → server messages)::

    {
        "type": "<message_type>",
        "session_id": "<uuid>",
        "captured_at": "<ISO-8601, set client-side>",
        "payload": { ... }
    }

The ``type`` discriminator selects which concrete ``payload`` model
applies.  The server handler parses the raw JSON into
:func:`parse_client_message`, which returns the appropriate typed
envelope.

**Design choices:**

- ``captured_at`` is the *client-side* wall-clock timestamp.  The
  server records its own ``received_at`` on ingestion so clock-skew
  analysis is possible without trusting the client's clock.
- ``session_id`` is redundant with the authenticated WebSocket's
  session binding — the server **must** verify it matches the
  token-derived session before processing.  The redundancy is
  intentional: it makes envelope-level logs self-describing even
  without the connection context.
- Audio payloads carry the ``sample_rate_hz`` and ``duration_ms``
  fields because ``webrtcvad`` requires exact frame sizes
  (10 / 20 / 30 ms at 8000 / 16000 / 32000 / 48000 Hz).
  The client is responsible for chunking to one of those
  combinations; the server rejects non-compliant frames.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any, Literal, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# ---------------------------------------------------------------------------
# Error surface
# ---------------------------------------------------------------------------

class ExamClientError(Exception):
    """Base exception for client-side protocol errors."""


class EnvelopeValidationError(ExamClientError):
    """Raised when a message envelope fails structural validation."""


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

# Valid sample rates for ``webrtcvad`` (all in Hz).
VALID_SAMPLE_RATES: frozenset[int] = frozenset({8000, 16000, 32000, 48000})

# Valid frame durations for ``webrtcvad`` (all in ms).
VALID_FRAME_DURATIONS: frozenset[int] = frozenset({10, 20, 30})

# Browser events the client may report (DOM-level, event-driven, no polling).
VALID_BROWSER_EVENTS: frozenset[str] = frozenset(
    {
        "visibilitychange",
        "blur",
        "focus",
        "fullscreenchange",
        "copy",
        "paste",
        "contextmenu",
    }
)

# Heavy-frame encodings the server accepts.
VALID_FRAME_ENCODINGS: frozenset[str] = frozenset({"jpeg", "png", "webp"})


# ---------------------------------------------------------------------------
# Client → server message types
# ---------------------------------------------------------------------------

class ClientMessageType(str, enum.Enum):
    """Discriminator values for client → server messages."""

    TELEMETRY_LIGHT = "telemetry_light"
    TELEMETRY_HEAVY_FRAME = "telemetry_heavy_frame"
    AUDIO_CHUNK = "audio_chunk"
    BROWSER_EVENT = "browser_event"
    KILL_SWITCH_ACK = "kill_switch_ack"


# ---------------------------------------------------------------------------
# Payload models
# ---------------------------------------------------------------------------

class TelemetryLightPayload(BaseModel):
    """Lightweight client-side detection result (face presence / count).

    This is *not* a raw frame — it is the result of client-side
    inference (face detection, head pose) transmitted on every capture
    tick.  No raw pixels cross the wire for this message type.
    """

    model_config = ConfigDict(extra="forbid")

    modality: Literal["face_presence"] = "face_presence"
    face_count: int = Field(..., ge=0, description="Number of faces detected by client-side model.")
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Detection confidence in [0, 1].",
    )
    bbox: list[float] | None = Field(
        default=None,
        min_length=4,
        max_length=4,
        description="Bounding box [x, y, w, h] of the primary face, normalised to [0,1].",
    )

    @field_validator("bbox")
    @classmethod
    def _bbox_values_normalised(cls, v: list[float] | None) -> list[float] | None:
        if v is not None:
            for component in v:
                if not (0.0 <= component <= 1.0):
                    raise ValueError(
                        f"Bounding-box component {component} is outside [0, 1]."
                    )
        return v


class TelemetryHeavyFramePayload(BaseModel):
    """A raw video frame at the sparse interval (default 2–3 s).

    Sent for server-side inference (identity, gaze, object detection).
    The ``frame`` field is a base64-encoded JPEG (or other accepted
    encoding).  Resolution and quality are tunable; a reasonable
    starting point is ~480 p with JPEG quality tuned for bandwidth.
    """

    model_config = ConfigDict(extra="forbid")

    frame: str = Field(
        ...,
        min_length=1,
        description="Base64-encoded image data.",
    )
    resolution: list[int] = Field(
        ...,
        min_length=2,
        max_length=2,
        description="[width, height] in pixels.",
    )
    encoding: str = Field(
        ...,
        description="Image encoding (jpeg, png, webp).",
    )

    @field_validator("resolution")
    @classmethod
    def _resolution_positive(cls, v: list[int]) -> list[int]:
        if v[0] <= 0 or v[1] <= 0:
            raise ValueError("Resolution width and height must be positive integers.")
        return v

    @field_validator("encoding")
    @classmethod
    def _encoding_valid(cls, v: str) -> str:
        if v not in VALID_FRAME_ENCODINGS:
            raise ValueError(
                f"Encoding '{v}' is not supported; use one of {sorted(VALID_FRAME_ENCODINGS)}."
            )
        return v


class TelemetryAudioChunkPayload(BaseModel):
    """A VAD-ready audio chunk for server-side voice activity detection.

    The client must resample / chunk to one of the exact combinations
    ``webrtcvad`` accepts:  sample rate ∈ {8000, 16000, 32000, 48000} Hz
    and frame duration ∈ {10, 20, 30} ms.  The server rejects
    non-compliant frames.
    """

    model_config = ConfigDict(extra="forbid")

    audio: str = Field(
        ...,
        min_length=1,
        description="Base64-encoded PCM or Opus audio data.",
    )
    sample_rate_hz: int = Field(
        ...,
        description="Sample rate in Hz; must be 8000 | 16000 | 32000 | 48000.",
    )
    duration_ms: int = Field(
        ...,
        description="Frame duration in ms; must be 10 | 20 | 30.",
    )

    @field_validator("sample_rate_hz")
    @classmethod
    def _rate_valid(cls, v: int) -> int:
        if v not in VALID_SAMPLE_RATES:
            raise ValueError(
                f"Sample rate {v} Hz is not supported by webrtcvad; "
                f"use one of {sorted(VALID_SAMPLE_RATES)}."
            )
        return v

    @field_validator("duration_ms")
    @classmethod
    def _duration_valid(cls, v: int) -> int:
        if v not in VALID_FRAME_DURATIONS:
            raise ValueError(
                f"Frame duration {v} ms is not supported by webrtcvad; "
                f"use one of {sorted(VALID_FRAME_DURATIONS)}."
            )
        return v


class TelemetryBrowserEventPayload(BaseModel):
    """A DOM-level signal captured event-driven by the exam client.

    Browser events are not polled — they fire on the actual DOM event
    (``visibilitychange``, ``blur``, ``focus``, ``fullscreenchange``,
    ``copy``, ``paste``, ``contextmenu``).  The ``detail`` bag carries
    any event-specific metadata the client may include (e.g.
    ``document.hidden`` state for ``visibilitychange``).
    """

    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(
        ...,
        description="DOM event type.",
    )
    detail: dict[str, Any] = Field(
        default_factory=dict,
        description="Event-specific metadata (e.g. hidden state).",
    )

    @field_validator("event_type")
    @classmethod
    def _event_type_valid(cls, v: str) -> str:
        if v not in VALID_BROWSER_EVENTS:
            raise ValueError(
                f"Browser event type '{v}' is not recognised; "
                f"use one of {sorted(VALID_BROWSER_EVENTS)}."
            )
        return v


# ---------------------------------------------------------------------------
# Kill-switch acknowledgement (client → server)
# ---------------------------------------------------------------------------

class KillSwitchAcknowledgePayload(BaseModel):
    """Sent by the client after receiving and processing a kill-switch.

    The ``flag_id`` must match the one in the server's ``kill_switch``
    message.  ``answered_questions`` lets the server record how much
    progress the student had at the time of termination.
    """

    model_config = ConfigDict(extra="forbid")

    flag_id: str = Field(
        ...,
        min_length=1,
        description="UUID of the flag that triggered the kill-switch.",
    )
    answered_questions: int | None = Field(
        default=None,
        ge=0,
        description="Number of questions the student had answered at termination time.",
    )


# ---------------------------------------------------------------------------
# Full envelope models (envelope + typed payload)
# ---------------------------------------------------------------------------

class _ClientEnvelopeBase(BaseModel):
    """Shared fields across all client → server envelopes."""

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(
        ...,
        min_length=1,
        description="UUID of the exam session (must match the token-derived session).",
    )
    captured_at: datetime = Field(
        ...,
        description="Client-side wall-clock timestamp (ISO-8601).",
    )


class TelemetryLight(_ClientEnvelopeBase):
    """Client → server: lightweight face-presence result."""

    type: Literal[ClientMessageType.TELEMETRY_LIGHT] = ClientMessageType.TELEMETRY_LIGHT
    payload: TelemetryLightPayload


class TelemetryHeavyFrame(_ClientEnvelopeBase):
    """Client → server: raw video frame for server-side inference."""

    type: Literal[ClientMessageType.TELEMETRY_HEAVY_FRAME] = ClientMessageType.TELEMETRY_HEAVY_FRAME
    payload: TelemetryHeavyFramePayload


class TelemetryAudioChunk(_ClientEnvelopeBase):
    """Client → server: VAD-ready audio chunk."""

    type: Literal[ClientMessageType.AUDIO_CHUNK] = ClientMessageType.AUDIO_CHUNK
    payload: TelemetryAudioChunkPayload


class TelemetryBrowserEvent(_ClientEnvelopeBase):
    """Client → server: DOM-level browser event."""

    type: Literal[ClientMessageType.BROWSER_EVENT] = ClientMessageType.BROWSER_EVENT
    payload: TelemetryBrowserEventPayload


class KillSwitchAcknowledge(_ClientEnvelopeBase):
    """Client → server: acknowledgement of a kill-switch delivery."""

    type: Literal[ClientMessageType.KILL_SWITCH_ACK] = ClientMessageType.KILL_SWITCH_ACK
    payload: KillSwitchAcknowledgePayload


# ---------------------------------------------------------------------------
# Discriminated union for parsing
# ---------------------------------------------------------------------------

ClientMessage = Union[
    TelemetryLight,
    TelemetryHeavyFrame,
    TelemetryAudioChunk,
    TelemetryBrowserEvent,
    KillSwitchAcknowledge,
]
"""Union of all client → server message types, discriminated on ``type``."""


# ---------------------------------------------------------------------------
# Envelope parser
# ---------------------------------------------------------------------------

# Map from the ``type`` discriminator to the concrete model.
_TYPE_TO_MODEL: dict[str, type[BaseModel]] = {
    ClientMessageType.TELEMETRY_LIGHT.value: TelemetryLight,
    ClientMessageType.TELEMETRY_HEAVY_FRAME.value: TelemetryHeavyFrame,
    ClientMessageType.AUDIO_CHUNK.value: TelemetryAudioChunk,
    ClientMessageType.BROWSER_EVENT.value: TelemetryBrowserEvent,
    ClientMessageType.KILL_SWITCH_ACK.value: KillSwitchAcknowledge,
}


def parse_client_message(raw: dict[str, Any]) -> ClientMessage:
    """Parse a raw JSON dict into the appropriate typed envelope.

    Raises :class:`EnvelopeValidationError` on any structural problem
    (unknown ``type``, missing fields, out-of-range values).
    """

    msg_type = raw.get("type")
    if msg_type is None:
        raise EnvelopeValidationError("Message envelope is missing the 'type' field.")

    model_cls = _TYPE_TO_MODEL.get(msg_type)
    if model_cls is None:
        raise EnvelopeValidationError(
            f"Unknown client message type '{msg_type}'; "
            f"expected one of {sorted(_TYPE_TO_MODEL.keys())}."
        )

    try:
        return model_cls.model_validate(raw)  # type: ignore[return-value]
    except Exception as exc:
        raise EnvelopeValidationError(
            f"Validation failed for message type '{msg_type}': {exc}"
        ) from exc
