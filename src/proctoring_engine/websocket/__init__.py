"""WebSocket protocol package.

Implements the authenticated WebSocket protocol layer for the AI Proctoring Engine.

This layer handles:
- Authenticated WebSocket connection establishment via session token
- Message envelope types (client → server and server → client)
- Telemetry frame ingestion (light, heavy, audio)
- Browser event capture
- Kill-switch delivery
- Per-frame acknowledgments

See ``docs/02-ingestion-layer-design.md`` §2-4 for the message contracts.
"""

from proctoring_engine.websocket.client import (
    ClientMessageType,
    EnvelopeValidationError,
    ExamClientError,
    KillSwitchAcknowledge,
    KillSwitchAcknowledgePayload,
    TelemetryAudioChunk,
    TelemetryAudioChunkPayload,
    TelemetryBrowserEvent,
    TelemetryBrowserEventPayload,
    TelemetryHeavyFrame,
    TelemetryHeavyFramePayload,
    TelemetryLight,
    TelemetryLightPayload,
    parse_client_message,
)
from proctoring_engine.websocket.server import (
    BufferedEvent,
    DeliveryService,
    KillSwitchDeliver,
    KillSwitchDeliverError,
    KillSwitchPayload,
    PolicyUpdateDeliver,
    PolicyUpdatePayload,
    ServerMessageType,
    SessionAcknowledgeDeliver,
    SessionAcknowledgePayload,
    TelemetryEventBuffer,
)
from proctoring_engine.websocket.routes import (
    WS_CLOSE_AUTH_EXPIRED,
    WS_CLOSE_AUTH_FAILED,
    WS_CLOSE_HEARTBEAT_TIMEOUT,
    WS_CLOSE_NOT_LEARNER,
    WS_CLOSE_PROTOCOL_ERROR,
    WS_CLOSE_SESSION_INVALID,
    WS_CLOSE_SESSION_MISMATCH,
    build_ws_router,
)

__all__ = [
    # Client envelope types
    "ClientMessageType",
    "EnvelopeValidationError",
    "ExamClientError",
    "KillSwitchAcknowledge",
    "KillSwitchAcknowledgePayload",
    "TelemetryAudioChunk",
    "TelemetryAudioChunkPayload",
    "TelemetryBrowserEvent",
    "TelemetryBrowserEventPayload",
    "TelemetryHeavyFrame",
    "TelemetryHeavyFramePayload",
    "TelemetryLight",
    "TelemetryLightPayload",
    "parse_client_message",
    # Server message types
    "BufferedEvent",
    "DeliveryService",
    "KillSwitchDeliver",
    "KillSwitchDeliverError",
    "KillSwitchPayload",
    "PolicyUpdateDeliver",
    "PolicyUpdatePayload",
    "ServerMessageType",
    "SessionAcknowledgeDeliver",
    "SessionAcknowledgePayload",
    "TelemetryEventBuffer",
    # Router + close codes
    "WS_CLOSE_AUTH_EXPIRED",
    "WS_CLOSE_AUTH_FAILED",
    "WS_CLOSE_HEARTBEAT_TIMEOUT",
    "WS_CLOSE_NOT_LEARNER",
    "WS_CLOSE_PROTOCOL_ERROR",
    "WS_CLOSE_SESSION_INVALID",
    "WS_CLOSE_SESSION_MISMATCH",
    "build_ws_router",
]
