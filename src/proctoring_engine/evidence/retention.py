"""Retention deletion worker for evidence artifacts.

Per ``docs/06-evidence-audit-store-design.md`` §3, a scheduled worker
(not part of the request-handling path) queries for expired rows,
deletes the object storage blob first, then the DB row. This ordering
ensures:

1. A row never points at a non-existent blob.
2. If the job is interrupted mid-run, the safer failure mode is
   "blob deleted but row still exists" (detectable via missing blob)
   rather than "row deleted but blob orphaned" (storage leak).

The deletion job is what makes ``retention_expires_at`` actually mean
something — the field alone is inert without a process that acts on it.

This module provides a pure function that can be called from a scheduler
(APScheduler, Celery beat, Kubernetes CronJob, etc.). The scheduler
integration is the orchestration layer's responsibility.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from proctoring_engine.evidence._protocol import (
    EvidenceNotFoundError,
    EvidenceStore,
    EvidenceStoreError,
)
from proctoring_engine.evidence._storage_key import parse_storage_key
from proctoring_engine.models import EvidenceArtifact

logger = logging.getLogger(__name__)

class RetentionJobDeps(Protocol):
    """Dependencies for the retention deletion job.

    Allows the job to be called with test doubles for the database
    session and evidence store.
    """

    @property
    def db(self) -> Session:
        """The database session for querying expired artifacts."""
        ...

    @property
    def store(self) -> EvidenceStore:
        """The evidence store for deleting blobs."""
        ...

@dataclass(frozen=True, slots=True)
class RetentionDeletionResult:
    """Result of a single retention deletion run.

    Attributes
    ----------
    artifacts_deleted:
        Number of ``EvidenceArtifact`` rows deleted.
    storage_errors:
        Number of blobs that failed to delete.
    """

    artifacts_deleted: int
    storage_errors: int

def run_retention_deletion(
    db: Session,
    store: EvidenceStore,
    now: datetime | None = None,
    batch_size: int = 100,
) -> RetentionDeletionResult:
    """Delete expired evidence artifacts.

    This function should be called periodically (e.g. every hour) by a
    scheduler. It processes expired rows in batches to avoid long-running
    transactions and memory pressure.

    The deletion order for each artifact is:

    1. Query for expired ``EvidenceArtifact`` rows.
    2. For each artifact, extract the storage key from ``storage_uri``.
    3. Delete the blob from object storage.
    4. If blob deletion succeeds, delete the DB row.
    5. If blob deletion fails, log the error but continue processing.

    Parameters
    ----------
    db:
        The database session.
    store:
        The evidence store for blob deletion.
    now:
        The current timestamp. If ``None``, uses ``datetime.now(timezone.utc)``.
        Passed for deterministic testing.
    batch_size:
        Maximum number of rows to process in a single run.

    Returns
    -------
    RetentionDeletionResult
        Counts of deleted rows and any storage errors.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    artifacts_deleted = 0
    storage_errors = 0
    # Track artifacts that failed blob deletion in this run so we don't
    # infinite-loop trying to delete a persistently failing blob.
    failed_artifact_ids: set[uuid.UUID] = set()

    # Process expired EvidenceArtifact rows.
    # We iterate in batches to avoid loading everything into memory.
    while True:
        stmt = select(EvidenceArtifact).where(
            EvidenceArtifact.retention_expires_at < now
        )
        if failed_artifact_ids:
            stmt = stmt.where(EvidenceArtifact.id.notin_(failed_artifact_ids))

        # Order by id to guarantee deterministic forward progress
        stmt = stmt.order_by(EvidenceArtifact.id).limit(batch_size)

        artifacts = db.execute(stmt).scalars().all()

        if not artifacts:
            break

        batch_deleted = 0
        for artifact in artifacts:
            # Extract the storage key from storage_uri.
            # storage_uri format: "s3://evidence/{session_id}/{flag_id}/{type}.{ext}"
            storage_key = _extract_storage_key(artifact.storage_uri)
            if storage_key is None:
                logger.warning(
                    "Could not parse storage_uri for artifact %s: %s",
                    artifact.id,
                    artifact.storage_uri,
                )
                storage_errors += 1
                failed_artifact_ids.add(artifact.id)
                continue

            # Delete the blob first.
            try:
                store.delete(storage_key)
            except EvidenceNotFoundError:
                # Blob already gone — this is fine, continue to delete the row.
                logger.debug(
                    "Blob already deleted for artifact %s at key %s",
                    artifact.id,
                    storage_key,
                )
            except EvidenceStoreError as exc:
                logger.error(
                    "Failed to delete blob for artifact %s at key %s: %s",
                    artifact.id,
                    storage_key,
                    exc,
                )
                storage_errors += 1
                failed_artifact_ids.add(artifact.id)
                continue

            # Delete the DB row.
            try:
                db.delete(artifact)
                db.flush()  # Flush per row to catch constraint violations early
                batch_deleted += 1
                artifacts_deleted += 1
            except Exception as exc:
                logger.error(
                    "Failed to delete EvidenceArtifact row %s: %s",
                    artifact.id,
                    exc,
                )
                db.rollback()
                failed_artifact_ids.add(artifact.id)
                continue

        # Commit artifact deletions per batch to avoid long-running transactions
        if batch_deleted > 0:
            try:
                db.commit()
            except Exception as exc:
                logger.error("Failed to commit artifact deletions for batch: %s", exc)
                db.rollback()
                # If the batch commit fails, we don't know which individual artifact
                # caused it. Back out the counter for this batch and break the run
                # to avoid infinite-looping on a poisoned database connection.
                artifacts_deleted -= batch_deleted
                break

    return RetentionDeletionResult(
        artifacts_deleted=artifacts_deleted,
        storage_errors=storage_errors,
    )

def _extract_storage_key(storage_uri: str) -> str | None:
    """Extract the storage key from a storage_uri.

    The storage_uri format is ``s3://evidence/...``. This function
    strips the ``s3://`` prefix and returns the rest as the key.

    Parameters
    ----------
    storage_uri:
        The full storage URI.

    Returns
    -------
    str | None
        The storage key, or ``None`` if the URI format is unexpected.
    """
    if storage_uri.startswith("s3://"):
        return storage_uri[5:]  # Strip "s3://"
    # Fallback: treat the whole URI as the key (for test fakes).
    if storage_uri.startswith("evidence/"):
        return storage_uri
    return None
