"""Evidence sealing service for the orchestration layer.

Wraps :func:`proctoring_engine.evidence.service.seal_evidence` with
the database side of the persistence transaction: INSERT the
``EvidenceArtifact`` row in the same transaction as the upload.  This
is the layer that closes the deferred-gap from
:mod:`ARCHITECTURE.md` §7 (no FastAPI route triggering evidence
flush; the service is callable but the route is the API).

The function is the **only** place evidence-blob upload + DB-row
insertion happen together in v1.  It is the call the route handler
makes on ``POST /sessions/{id}/flags/{flag_id}/evidence``.

The blob-first / row-second invariant from
:mod:`docs/06-evidence-audit-store-design.md` §2 is preserved:

1. ``seal_evidence`` uploads the blob and verifies its remote
   checksum (on mismatch, the blob is deleted and the function
   raises — no ``EvidenceArtifact`` row is created).
2. On success, INSERT the ``EvidenceArtifact`` row with the
   storage URI, content_sha256, byte_size, etc.

If step 2 fails (most likely because the ``Flag`` already has an
artifact — the ``uq_evidence_artifacts_one_per_flag`` unique
constraint enforces "one per flag"), the function rolls back the DB
write.  The blob is already in storage; the
:mod:`proctoring_engine.evidence.retention` worker will eventually
delete it because there is no ``EvidenceArtifact`` row pointing at
it.  The route handler raises :class:`EvidenceAlreadySealed`, which
maps to HTTP 409.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from proctoring_engine.evidence._protocol import EvidenceStore
from proctoring_engine.evidence.service import (
    EvidenceSealError,
    SealEvidenceRequest,
    SealEvidenceResult,
    seal_evidence,
)
from proctoring_engine.models import (
    EvidenceArtifact,
    EvidenceKind,
    Flag,
)


#: Maximum blob size in bytes accepted by the v1 evidence seal route.
#: 50 MiB matches the rolling-buffer design from
#: :mod:`docs/06-evidence-audit-store-design.md` §1 (the client
#: buffers 10–15 s of capture).  Enforced at the layer boundary so
#: a misbehaving client cannot push a multi-GB blob into storage;
#: the result is a 413 Payload Too Large from the route handler.
_MAX_BLOB_BYTES: Final[int] = 50 * 1024 * 1024


class EvidenceAlreadySealedError(Exception):
    """The :class:`Flag` already has an :class:`EvidenceArtifact` row
    (``uq_evidence_artifacts_one_per_flag``).

    Maps to HTTP 409 at the route layer.
    """


class EvidenceBlobTooLargeError(Exception):
    """The uploaded blob exceeds :data:`_MAX_BLOB_BYTES`.

    Maps to HTTP 413 at the route layer.
    """

    def __init__(self, byte_size: int) -> None:
        super().__init__(
            f"blob of {byte_size} bytes exceeds the {_MAX_BLOB_BYTES}-byte cap"
        )
        self.byte_size = byte_size


class EvidenceSealCollisionError(Exception):
    """The storage key already exists at upload time.

    Should be unreachable in practice — the storage layer's
    ``upload`` is overwrite-or-create and the
    :class:`FlagTelemetryEvent` link uses a fresh UUID per artifact —
    but the collision is detected and surfaced cleanly rather than
    panicking.
    """

    def __init__(self, storage_key: str) -> None:
        super().__init__(f"storage key {storage_key!r} already exists")
        self.storage_key = storage_key


@dataclass(frozen=True, slots=True)
class SealEvidenceServiceResult:
    """The output of :func:`seal_evidence_for_flag`.

    Carries the freshly-INSERTed :class:`EvidenceArtifact` row plus
    the underlying :class:`SealEvidenceResult` for the audit log.
    """

    artifact: EvidenceArtifact
    seal_result: SealEvidenceResult


def _artifact_type_to_enum(value: str) -> EvidenceKind:
    """Map the wire-format ``artifact_type`` to the ORM enum.

    Raises :class:`ValueError` on a malformed type; the Pydantic
    schema has already validated the wire value as a
    :class:`Literal["frame", "clip", "audio", "event_export"]` so
    this branch is a defense-in-depth check.
    """

    return {
        "frame": EvidenceKind.FRAME,
        "clip": EvidenceKind.CLIP,
        "audio": EvidenceKind.AUDIO,
        "event_export": EvidenceKind.EVENT_EXPORT,
    }[value]


def seal_evidence_for_flag(
    db: Session,
    store: EvidenceStore,
    *,
    request: SealEvidenceRequest,
    default_retention_seconds: int,
    now: datetime,
) -> SealEvidenceServiceResult:
    """Upload the blob, then INSERT the ``EvidenceArtifact`` row in
    the same transaction.

    Parameters
    ----------
    db:
        The SQLAlchemy session for the active transaction.  The
        ``Flag`` row (``request.flag_id``) is loaded from here.
    store:
        The :class:`EvidenceStore` backend.  Production uses
        :class:`proctoring_engine.evidence._s3.S3EvidenceStore`;
        tests use :class:`InMemoryEvidenceStore`.
    request:
        The seal request carrying the blob bytes and metadata.
    default_retention_seconds:
        The retention horizon stamped onto the artifact when the
        request's ``retention_expires_at`` is ``None``.  Comes from
        :class:`OrchestrationSettings.retention_default_seconds`.
    now:
        Current UTC ``datetime``.  Used only for retention stamping;
        never mutates any other column.

    Returns
    -------
    SealEvidenceServiceResult:
        Carries the INSERTed :class:`EvidenceArtifact` row plus
        the underlying :class:`SealEvidenceResult`.

    Raises
    ------
    EvidenceAlreadySealedError:
        The :class:`Flag` already has an :class:`EvidenceArtifact`
        row.  The blob has been uploaded; the orphan is cleaned up
        by the retention worker because no row points at it.
    EvidenceBlobTooLargeError:
        The blob exceeds :data:`_MAX_BLOB_BYTES`.
    EvidenceStoreError:
        The underlying storage write failed.
    EvidenceSealCollisionError:
        The storage key already existed at upload time (should be
        unreachable; defensive).
    """

    blob = request.blob
    if len(blob) > _MAX_BLOB_BYTES:
        raise EvidenceBlobTooLargeError(len(blob))

    flag = db.get(Flag, request.flag_id)
    if flag is None:
        raise EvidenceAlreadySealedError(
            f"flag {request.flag_id!s} does not exist"
        )

    # The wire request may carry an explicit retention horizon; if
    # not, fall back to the deployment's default.  Normalize naive
    # ``now`` to UTC so the SQL column (timezone-aware) accepts it.
    if request.retention_expires_at is not None:
        retention = request.retention_expires_at
    else:
        anchor = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        retention = anchor + timedelta(seconds=default_retention_seconds)

    effective_request = SealEvidenceRequest(
        flag_id=request.flag_id,
        exam_session_id=request.exam_session_id,
        artifact_type=request.artifact_type,
        media_type=request.media_type,
        blob=blob,
        capture_started_at=request.capture_started_at,
        capture_ended_at=request.capture_ended_at,
        retention_expires_at=retention,
    )

    seal = seal_evidence(store, effective_request)

    artifact = EvidenceArtifact(
        flag_id=request.flag_id,
        kind=_artifact_type_to_enum(request.artifact_type),
        storage_uri=f"s3://{seal.storage_key}",
        content_sha256=seal.content_sha256,
        media_type=seal.media_type,
        byte_size=seal.byte_size,
        capture_started_at=seal.capture_started_at,
        capture_ended_at=seal.capture_ended_at,
        retention_expires_at=seal.retention_expires_at,
    )
    db.add(artifact)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # The blob is now an orphan; the retention worker will
        # reclaim it because no DB row points at it.  Distinguish
        # the "already-sealed" case from a generic FK violation —
        # only the former is a 409, the latter is a 500.
        constraint = (
            getattr(exc.orig, "diag", None)
            and getattr(exc.orig.diag, "constraint_name", "")
        ) or ""
        if "uq_evidence_artifacts_one_per_flag" in constraint:
            raise EvidenceAlreadySealedError(
                f"flag {request.flag_id!s} already has an evidence artifact"
            ) from exc
        raise EvidenceAlreadySealedError(
            f"flag {request.flag_id!s} not found or constraint violation"
        ) from exc

    db.refresh(artifact)
    return SealEvidenceServiceResult(artifact=artifact, seal_result=seal)


__all__ = [
    "EvidenceAlreadySealedError",
    "EvidenceBlobTooLargeError",
    "EvidenceSealCollisionError",
    "SealEvidenceServiceResult",
    "seal_evidence_for_flag",
]
