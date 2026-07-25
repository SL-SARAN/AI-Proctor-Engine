"""Accommodation-exemption suppression logic.

Before the fusion engine finalises a ``Flag`` involving an object
class, it checks the participant's ``AccommodationExemption`` records.
If a matching exemption is found, the flag's severity is downgraded
(never silently dropped — the underlying ``TelemetryEvent`` remains in
the audit trail) and ``Flag.suppressed_by_exemption_id`` is set so the
record shows that an exemption was checked and applied.

The approach follows ``docs/05-fusion-flagging-engine-design.md``
§"Exemption suppression" — downgrade-and-log rather than silent drop,
because the detection event should always be logged regardless.

This module is a **pure function with no DB access**.  The caller
(the ``SessionAggregator`` or the orchestration layer) is responsible
for loading the relevant exemptions and passing them in.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final


# ---------------------------------------------------------------------------
# Lightweight exemption record (not the ORM model)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ExemptionRecord:
    """A denormalised snapshot of an ``AccommodationExemption`` row.

    The ``SessionAggregator`` receives these at construction time (or
    when the session starts) so it can check exemptions without hitting
    the database on every frame.  The fields are the subset needed for
    the match logic.
    """

    id: uuid.UUID
    participant_id: uuid.UUID
    object_class: str
    exam_reference: str
    effective_at: datetime
    expires_at: datetime | None = None


# ---------------------------------------------------------------------------
# Severity constants for suppressed flags
# ---------------------------------------------------------------------------

SUPPRESSED_SEVERITY: Final[str] = "low"
"""Severity to which an exempted flag is downgraded.

A flag whose object class matches an active accommodation exemption is
*not* dropped — it is recorded at ``LOW`` severity with
``suppressed_by_exemption_id`` set.  This preserves the audit trail
while preventing the detection from contributing to escalation or
accumulation.
"""


# ---------------------------------------------------------------------------
# Match logic
# ---------------------------------------------------------------------------

def find_matching_exemption(
    participant_id: uuid.UUID,
    object_class: str,
    exam_reference: str,
    now: datetime,
    exemptions: list[ExemptionRecord],
) -> ExemptionRecord | None:
    """Find the first active exemption matching the detection.

    Parameters
    ----------
    participant_id:
        The ``Participant.id`` of the test-taker.
    object_class:
        The detected object class (e.g. ``"cell phone"``).
    exam_reference:
        The ``ExamSession.exam_reference`` string, used to match
        exemptions scoped to a specific exam.
    now:
        The current UTC timestamp, used to check ``effective_at``
        and ``expires_at``.
    exemptions:
        Pre-loaded exemption records for this participant.

    Returns
    -------
    ExemptionRecord | None
        The matching exemption, or ``None`` if no exemption applies.
        If multiple exemptions match, the first one found is returned
        (the order of ``exemptions`` is the caller's responsibility).
    """

    for ex in exemptions:
        if ex.participant_id != participant_id:
            continue
        if ex.object_class != object_class:
            continue
        if ex.exam_reference != exam_reference:
            continue
        if now < ex.effective_at:
            continue
        if ex.expires_at is not None and now >= ex.expires_at:
            continue
        return ex

    return None
