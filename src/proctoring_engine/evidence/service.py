"""Evidence sealing service.

This module implements the core "flush-to-storage" flow from
``docs/06-evidence-audit-store-design.md`` §2:

1. Fusion engine confirms a ``Flag``.
2. Server tells the client to flush its rolling buffer.
3. Client uploads the buffered clip.
4. Server writes the blob to object storage, computes a checksum,
   and *only then* inserts the ``EvidenceArtifact`` row.
5. ``capture_started_at`` / ``capture_ended_at`` are set from the
   buffer's real timestamps (from the client), not server receipt time.

The service is a pure function: it takes the blob bytes and metadata,
uploads via the ``EvidenceStore``, computes the checksum, and returns
a dict of field values for the orchestration layer to persist.
The service does **not** perform the DB insert itself — that is the
orchestration layer's job, allowing the service to remain testable
without a database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from proctoring_engine.evidence._checksum import compute_sha256
from proctoring_engine.evidence._protocol import (
    EvidenceStore,
    EvidenceStoreError,
)
from proctoring_engine.evidence._storage_key import build_storage_key


@dataclass(frozen=True, slots=True)
class SealEvidenceRequest:
    """Input to the :func:`seal_evidence` service.

    Attributes
    ----------
    flag_id:
        The ``Flag.id`` this evidence is attached to.
    exam_session_id:
        The ``ExamSession.id`` this evidence belongs to.
    artifact_type:
        One of ``frame``, ``clip``, ``audio``, ``event_export``.
    media_type:
        MIME type of the blob (e.g. ``video/webm``, ``image/jpeg``).
    blob:
        The raw evidence content.
    capture_started_at:
        When the client started capturing this evidence (from the client's clock).
    capture_ended_at:
        When the client stopped capturing (from the client's clock).
    retention_expires_at:
        When this evidence should be deleted per policy.
    """

    flag_id: uuid.UUID
    exam_session_id: uuid.UUID
    artifact_type: str
    media_type: str
    blob: bytes
    capture_started_at: datetime
    capture_ended_at: datetime | None
    retention_expires_at: datetime


@dataclass(frozen=True, slots=True)
class SealEvidenceResult:
    """Output from the :func:`seal_evidence` service.

    Contains all fields needed to insert an ``EvidenceArtifact`` row,
    plus the storage key for auditing.
    """

    storage_key: str
    content_sha256: str
    byte_size: int
    media_type: str
    capture_started_at: datetime
    capture_ended_at: datetime | None
    retention_expires_at: datetime

    def to_orm_kwargs(self, flag_id: uuid.UUID) -> dict[str, Any]:
        """Convert to kwargs suitable for ``EvidenceArtifact(**kwargs)``.

        The caller still needs to set ``flag_id`` (passed separately
        because it's already on the request).
        """
        return {
            "flag_id": flag_id,
            "kind": self._artifact_type_to_kind(),
            "storage_uri": f"s3://{self.storage_key}",
            "content_sha256": self.content_sha256,
            "media_type": self.media_type,
            "byte_size": self.byte_size,
            "capture_started_at": self.capture_started_at,
            "capture_ended_at": self.capture_ended_at,
            "retention_expires_at": self.retention_expires_at,
        }

    def _artifact_type_to_kind(self) -> str:
        """Map artifact_type to EvidenceKind enum value."""
        # The artifact_type matches EvidenceKind values directly
        # except for "clip" which maps to EvidenceKind.CLIP
        type_to_kind = {
            "frame": "frame",
            "clip": "clip",
            "audio": "audio",
            "event_export": "event_export",
        }
        return type_to_kind.get(self.artifact_type, self.artifact_type)

    artifact_type: str


class EvidenceSealError(Exception):
    """Raised when evidence sealing fails."""


def seal_evidence(
    store: EvidenceStore,
    request: SealEvidenceRequest,
) -> SealEvidenceResult:
    """Upload evidence and prepare the artifact record.

    This function implements the "blob-first, row-second" invariant:
    the blob is uploaded to object storage before any DB row is created.
    If the upload fails, no DB row should be inserted.

    The caller (orchestration layer) is responsible for:
    1. Starting a database transaction.
    2. Calling this function.
    3. Inserting the ``EvidenceArtifact`` row on success.
    4. Committing the transaction.

    If the transaction rolls back after the upload succeeds, the blob
    will be orphaned in storage. A periodic cleanup job (not implemented
    here) can detect and delete orphaned blobs.

    Parameters
    ----------
    store:
        The evidence store backend (S3 or in-memory for tests).
    request:
        The seal request containing blob and metadata.

    Returns
    -------
    SealEvidenceResult
        All fields needed to persist the ``EvidenceArtifact`` row.

    Raises
    ------
    EvidenceSealError
        If the upload or checksum computation fails.
    """
    # Build the storage key.
    try:
        storage_key = build_storage_key(
            exam_session_id=request.exam_session_id,
            flag_id=request.flag_id,
            artifact_type=request.artifact_type,
        )
    except ValueError as exc:
        raise EvidenceSealError(f"Invalid artifact_type: {exc}") from exc

    # Compute checksum locally before upload.
    content_sha256 = compute_sha256(request.blob)
    byte_size = len(request.blob)

    # Upload the blob.
    try:
        store.upload(storage_key, request.blob)
    except EvidenceStoreError as exc:
        raise EvidenceSealError(f"Failed to upload evidence: {exc}") from exc

    # Verify the upload by computing the checksum remotely.
    # This catches any silent corruption during upload.
    try:
        remote_sha256 = store.compute_checksum(storage_key)
    except EvidenceStoreError as exc:
        raise EvidenceSealError(f"Failed to verify upload: {exc}") from exc

    if remote_sha256 != content_sha256:
        # Checksum mismatch — delete the corrupted blob and fail.
        try:
            store.delete(storage_key)
        except EvidenceStoreError:
            # Log but don't mask the original error.
            pass
        raise EvidenceSealError(
            f"Checksum mismatch after upload: expected {content_sha256}, "
            f"got {remote_sha256}"
        )

    return SealEvidenceResult(
        storage_key=storage_key,
        content_sha256=content_sha256,
        byte_size=byte_size,
        media_type=request.media_type,
        capture_started_at=request.capture_started_at,
        capture_ended_at=request.capture_ended_at,
        retention_expires_at=request.retention_expires_at,
        artifact_type=request.artifact_type,
    )
