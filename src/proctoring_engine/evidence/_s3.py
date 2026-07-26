"""S3-compatible evidence store implementation.

Uses ``boto3`` to talk to any S3-compatible backend:
- Cloudflare R2 in production (zero egress fees).
- MinIO in local development (docker-compose).

The implementation is deliberately simple: no multipart uploads,
no presigned URLs, no ACLs. Evidence blobs are small (10–15s video
clips), so a single PUT is sufficient. The bucket is created on
first use if it does not exist.
"""

from __future__ import annotations

import logging
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from proctoring_engine.evidence._checksum import compute_sha256
from proctoring_engine.evidence._protocol import (
    EvidenceNotFoundError,
    EvidenceStore,
    EvidenceStoreError,
)
from proctoring_engine.evidence._settings import EvidenceStoreSettings

logger = logging.getLogger(__name__)


class S3EvidenceStore:
    """S3-compatible evidence blob storage.

    This class is the production implementation of the :class:`EvidenceStore`
    protocol. It connects to the endpoint specified in ``settings``,
    creates the bucket if necessary, and provides upload/download/delete
    operations.

    The class is not thread-safe; create one instance per worker process.

    Attributes
    ----------
    settings:
        The evidence store configuration.
    bucket:
        The bucket name (from settings).
    """

    def __init__(self, settings: EvidenceStoreSettings) -> None:
        self._settings = settings
        self._bucket = settings.bucket

        # Build the boto3 client with the provided settings.
        # R2 and MinIO both use path-style addressing (not virtual-host style).
        config = Config(
            connect_timeout=settings.connect_timeout_seconds,
            read_timeout=settings.read_timeout_seconds,
            retries={"max_attempts": 3, "mode": "standard"},
        )

        self._client: Any = boto3.client(
            "s3",
            endpoint_url=settings.endpoint_url,
            aws_access_key_id=settings.access_key,
            aws_secret_access_key=settings.secret_key,
            region_name=settings.region,
            config=config,
        )

        # Ensure the bucket exists (create-on-first-use).
        self._ensure_bucket_exists()

    @property
    def settings(self) -> EvidenceStoreSettings:
        return self._settings

    @property
    def bucket(self) -> str:
        return self._bucket

    def _ensure_bucket_exists(self) -> None:
        """Create the evidence bucket if it does not exist.

        Called once at construction. Safe to call multiple times —
        the ``HeadBucket`` check is cheap.

        Raises
        ------
        EvidenceStoreError
            If the bucket check or creation fails.
        """
        try:
            self._client.head_bucket(Bucket=self._bucket)
            logger.debug("Evidence bucket %s exists", self._bucket)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code == "404":
                # Bucket does not exist; create it.
                try:
                    self._client.create_bucket(Bucket=self._bucket)
                    logger.info("Created evidence bucket %s", self._bucket)
                except (ClientError, BotoCoreError) as create_exc:
                    raise EvidenceStoreError(
                        f"Failed to create bucket {self._bucket}: {create_exc}"
                    ) from create_exc
            else:
                raise EvidenceStoreError(
                    f"Failed to check bucket {self._bucket}: {exc}"
                ) from exc
        except BotoCoreError as exc:
            raise EvidenceStoreError(
                f"Failed to check bucket {self._bucket}: {exc}"
            ) from exc

    def upload(self, key: str, data: bytes) -> None:
        """Upload a blob to S3.

        Parameters
        ----------
        key:
            The storage key.
        data:
            The blob content.

        Raises
        ------
        EvidenceStoreError
            If the upload fails.
        """
        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=data)
            logger.debug("Uploaded %d bytes to %s", len(data), key)
        except (ClientError, BotoCoreError) as exc:
            raise EvidenceStoreError(f"Failed to upload {key}: {exc}") from exc

    def download(self, key: str) -> bytes:
        """Download a blob from S3.

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
            If the object does not exist.
        EvidenceStoreError
            If the download fails for another reason.
        """
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body = response["Body"].read()
            logger.debug("Downloaded %d bytes from %s", len(body), key)
            return body
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("NoSuchKey", "404"):
                raise EvidenceNotFoundError(f"No object at key {key}") from exc
            raise EvidenceStoreError(f"Failed to download {key}: {exc}") from exc
        except BotoCoreError as exc:
            raise EvidenceStoreError(f"Failed to download {key}: {exc}") from exc

    def delete(self, key: str) -> None:
        """Delete a blob from S3.

        Idempotent: deleting a non-existent key succeeds silently.

        Parameters
        ----------
        key:
            The storage key.

        Raises
        ------
        EvidenceStoreError
            If the delete fails.
        """
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
            logger.debug("Deleted %s", key)
        except (ClientError, BotoCoreError) as exc:
            raise EvidenceStoreError(f"Failed to delete {key}: {exc}") from exc

    def exists(self, key: str) -> bool:
        """Check whether a blob exists at the given key.

        Parameters
        ----------
        key:
            The storage key.

        Returns
        -------
        bool
            ``True`` if the object exists, ``False`` otherwise.
        """
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code == "404":
                return False
            raise EvidenceStoreError(f"Failed to check {key}: {exc}") from exc
        except BotoCoreError as exc:
            raise EvidenceStoreError(f"Failed to check {key}: {exc}") from exc

    def compute_checksum(self, key: str) -> str:
        """Compute the SHA-256 checksum of a blob.

        Downloads the blob and computes the checksum locally. This is
        the simplest approach and is acceptable for small blobs. For
        larger objects, S3 server-side checksums could be used instead.

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
            If the object does not exist.
        EvidenceStoreError
            If the checksum computation fails.
        """
        data = self.download(key)
        return compute_sha256(data)


class InMemoryEvidenceStore:
    """In-memory evidence store for unit testing.

    Implements the :class:`EvidenceStore` protocol without any external
    dependencies. Stores blobs in a plain ``dict``. Not suitable for
    production use — data is lost when the process exits.

    Thread-safe via a ``threading.Lock``.
    """

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._blobs: dict[str, bytes] = {}

    def upload(self, key: str, data: bytes) -> None:
        with self._lock:
            self._blobs[key] = data

    def download(self, key: str) -> bytes:
        with self._lock:
            if key not in self._blobs:
                raise EvidenceNotFoundError(f"No object at key {key}")
            return self._blobs[key]

    def delete(self, key: str) -> None:
        with self._lock:
            self._blobs.pop(key, None)

    def exists(self, key: str) -> bool:
        with self._lock:
            return key in self._blobs

    def compute_checksum(self, key: str) -> str:
        data = self.download(key)
        return compute_sha256(data)

    def list_keys(self) -> list[str]:
        """List all stored keys (test helper)."""
        with self._lock:
            return list(self._blobs.keys())

    def get_blob_count(self) -> int:
        """Return the number of stored blobs (test helper)."""
        with self._lock:
            return len(self._blobs)
