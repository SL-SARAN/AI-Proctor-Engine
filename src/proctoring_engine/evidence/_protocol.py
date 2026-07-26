"""Evidence store protocol defining the storage contract.

The protocol is the boundary between the evidence-sealing service and
the actual storage backend (S3 / R2 / MinIO). Production uses
:class:`S3EvidenceStore`; unit tests use :class:`InMemoryEvidenceStore`
or a mock.

The contract is intentionally narrow: upload a blob, download it,
delete it, check existence, and compute a checksum. No bucket creation,
no ACLs, no presigned URLs — those are deployment concerns, not API
layer concerns.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EvidenceStore(Protocol):
    """Protocol for S3-compatible evidence blob storage.

    All methods accept and return ``bytes`` for blob content. The caller
    (the evidence sealing service) is responsible for:

    1. Building the storage key via :func:`build_storage_key`.
    2. Computing the SHA-256 checksum (or delegating to ``compute_checksum``).
    3. Persisting the ``EvidenceArtifact`` row after the upload succeeds.

    The storage backend is responsible for:

    1. Connecting to the S3-compatible endpoint.
    2. Ensuring the bucket exists (create-on-first-use).
    3. Handling retries on transient network errors.

    Methods
    -------
    upload(key, data):
        Store ``data`` at ``key``. Overwrites if the key already exists.
    download(key):
        Retrieve the blob at ``key``.
    delete(key):
        Remove the blob at ``key``.
    exists(key):
        Return ``True`` if a blob exists at ``key``.
    compute_checksum(key):
        Compute SHA-256 checksum of the blob at ``key``.
    """

    def upload(self, key: str, data: bytes) -> None:
        """Upload a blob to the storage backend.

        Parameters
        ----------
        key:
            The storage key (e.g. ``evidence/{session_id}/{flag_id}/video_clip.webm``).
        data:
            The blob content.

        Raises
        ------
        EvidenceStoreError
            If the upload fails after retries.
        """
        ...

    def download(self, key: str) -> bytes:
        """Download a blob from the storage backend.

        Parameters
        ----------
        key:
            The storage key.

        Returns
        -------
        bytes
            The blob content.

        Raises
        ------
        EvidenceNotFoundError
            If no blob exists at ``key``.
        EvidenceStoreError
            If the download fails after retries.
        """
        ...

    def delete(self, key: str) -> None:
        """Delete a blob from the storage backend.

        Idempotent: deleting a non-existent key succeeds silently.

        Parameters
        ----------
        key:
            The storage key.

        Raises
        ------
        EvidenceStoreError
            If the delete fails after retries.
        """
        ...

    def exists(self, key: str) -> bool:
        """Check whether a blob exists at the given key.

        Parameters
        ----------
        key:
            The storage key.

        Returns
        -------
        bool
            ``True`` if a blob exists at ``key``, ``False`` otherwise.
        """
        ...

    def compute_checksum(self, key: str) -> str:
        """Compute the SHA-256 checksum of a blob.

        Parameters
        ----------
        key:
            The storage key.

        Returns
        -------
        str
            Lowercase hex-encoded SHA-256 digest.

        Raises
        ------
        EvidenceNotFoundError
            If no blob exists at ``key``.
        EvidenceStoreError
            If the checksum computation fails.
        """
        ...


class EvidenceStoreError(Exception):
    """Base exception for evidence store operations."""


class EvidenceNotFoundError(EvidenceStoreError):
    """Raised when attempting to download or checksum a non-existent blob."""
