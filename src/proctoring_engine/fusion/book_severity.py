"""Book-detection severity resolution.

Object detection always logs a book detection regardless of exam type
(per ``docs/04-inference-modules-design.md`` and
``docs/05-fusion-flagging-engine-design.md`` §"Book-detection severity
check").  The severity decision happens here: the aggregator checks
``ExamSession.allowed_reference_materials``:

- ``CLOSED_BOOK`` → ``MEDIUM`` severity flag.
- ``OPEN_BOOK`` → no flag raised (detection stays in the
  ``TelemetryEvent`` audit trail only).
- ``SPECIFIC_LIST`` → ``MEDIUM`` flag only if ``"book"`` is NOT in
  the session's ``permitted_material_details["allowed_items"]`` list.
  If the book is explicitly allowed, no flag.

This module is a **pure function** — no DB access, no side effects.
"""

from __future__ import annotations

from typing import Any, Final


# ---------------------------------------------------------------------------
# Severity for a book flag when it fires
# ---------------------------------------------------------------------------

BOOK_FLAG_SEVERITY: Final[str] = "medium"
"""Severity for a book detection that escalates to a flag."""

BOOK_RULE_CODE: Final[str] = "book_detected"
"""Rule code for a book detection flag."""


# ---------------------------------------------------------------------------
# Resolution logic
# ---------------------------------------------------------------------------

def should_flag_book(
    reference_material_policy: str,
    permitted_material_details: dict[str, Any],
) -> bool:
    """Decide whether a book detection should escalate to a flag.

    Parameters
    ----------
    reference_material_policy:
        The ``ReferenceMaterialPolicy`` value from the session
        (``"closed_book"``, ``"open_book"``, or ``"specific_list"``).
    permitted_material_details:
        The ``ExamSession.permitted_material_details`` JSONB payload.
        For ``SPECIFIC_LIST``, expected shape:
        ``{"allowed_items": ["book", ...]}``.

    Returns
    -------
    bool
        ``True`` if a ``Flag`` should be created; ``False`` if the
        detection should be logged in telemetry only (no flag).
    """

    if reference_material_policy == "closed_book":
        return True

    if reference_material_policy == "open_book":
        return False

    if reference_material_policy == "specific_list":
        allowed = permitted_material_details.get("allowed_items", [])
        if isinstance(allowed, list) and "book" in allowed:
            return False
        return True

    # Unknown policy — defensive: flag it so a human reviews.
    return True
