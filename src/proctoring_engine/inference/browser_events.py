"""Browser-event inference module (deterministic, no ML).

Native DOM listeners (``visibilitychange``, ``blur``/``focus``,
``fullscreenchange``, ``copy``/``paste``/``contextmenu``) fire
client-side and are forwarded as ``browser_event`` envelope messages.
There is no inference step beyond "this DOM event fired" —
``confidence`` is always ``1.0`` since there is no model uncertainty
in a deterministic signal.

The valid event set is imported from the WebSocket client envelope
layer (``proctoring_engine.websocket.client.VALID_BROWSER_EVENTS``)
so there is a single source of truth for which events the system
accepts.

Input:  ``event_type: str`` + ``detail: dict`` from the client
        envelope.
Output: a :class:`BrowserEventResult`.
"""

from __future__ import annotations

from typing import Any, Final

from proctoring_engine.inference._types import (
    BrowserEventResult,
    ConfidenceInterval,
)
from proctoring_engine.websocket.client import VALID_BROWSER_EVENTS


_MODALITY: Final[str] = "browser"

# Deterministic confidence — no model uncertainty.
_DETERMINISTIC_CONFIDENCE: Final[ConfidenceInterval] = ConfidenceInterval(
    lower=1.0, score=1.0, upper=1.0,
)


def classify_browser_event(
    event_type: str,
    detail: dict[str, Any] | None = None,
) -> BrowserEventResult:
    """Classify a DOM-level browser event.

    Parameters
    ----------
    event_type:
        The DOM event type (e.g. ``"visibilitychange"``).  Must be
        one of the recognised event types defined in the WebSocket
        client envelope layer.
    detail:
        Optional event-specific metadata from the client (e.g.
        ``{"hidden": True}`` for ``visibilitychange``).

    Returns
    -------
    BrowserEventResult
        ``confidence.score`` is always ``1.0``.

    Raises
    ------
    ValueError
        If ``event_type`` is not a recognised browser event.
    """

    if event_type not in VALID_BROWSER_EVENTS:
        raise ValueError(
            f"Unrecognised browser event type '{event_type}'; "
            f"expected one of {sorted(VALID_BROWSER_EVENTS)}."
        )

    if detail is None:
        detail = {}

    return BrowserEventResult(
        modality=_MODALITY,
        event_type=event_type,
        confidence=_DETERMINISTIC_CONFIDENCE,
        bounding_boxes=[],
        raw_value={"event_type": event_type, "detail": detail},
        detail=detail,
    )
