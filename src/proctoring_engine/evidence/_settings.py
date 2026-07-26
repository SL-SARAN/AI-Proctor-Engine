"""Evidence store configuration loaded from environment variables.

Mirrors the pattern used by :mod:`proctoring_engine.lti.config` — a
frozen dataclass populated from process environment variables, with
clear error messages when required values are missing.

The same variables are consumed by the docker-compose.yml ``app``
service (MinIO local stand-in) and the k8s Secret/ConfigMap (Cloudflare
R2 in production). The S3 API is interchangeable between the two.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class EvidenceStoreSettings:
    """S3-compatible storage configuration for evidence blobs.

    Attributes
    ----------
    endpoint_url:
        The S3 API endpoint. In production this is the Cloudflare R2 URL
        (``https://<accountid>.r2.cloudflarestorage.com``); locally it is
        ``http://minio:9000`` (or ``http://localhost:9000`` outside Docker).
    access_key:
        S3 access key ID. For R2 this is an API token scoped to a single
        bucket; for MinIO it is the ``MINIO_ROOT_USER`` value.
    secret_key:
        S3 secret access key.
    bucket:
        The bucket name for evidence artifacts. Created on first startup
        if it does not exist.
    region:
        S3 region. R2 uses ``auto``; MinIO uses ``us-east-1`` (the MinIO
        default).
    connect_timeout_seconds:
        Timeout for establishing the S3 connection.
    read_timeout_seconds:
        Timeout for S3 read operations (download, head).
    """

    endpoint_url: str
    access_key: str
    secret_key: str
    bucket: str
    region: str = "auto"
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 30.0


# Environment variable names (kept as module constants for testability)
_S3_ENDPOINT_URL: Final[str] = "S3_ENDPOINT_URL"
_S3_ACCESS_KEY: Final[str] = "S3_ACCESS_KEY"
_S3_SECRET_KEY: Final[str] = "S3_SECRET_KEY"
_S3_BUCKET: Final[str] = "S3_BUCKET"
_S3_REGION: Final[str] = "S3_REGION"
_S3_CONNECT_TIMEOUT_SECONDS: Final[str] = "S3_CONNECT_TIMEOUT_SECONDS"
_S3_READ_TIMEOUT_SECONDS: Final[str] = "S3_READ_TIMEOUT_SECONDS"


def get_evidence_store_settings() -> EvidenceStoreSettings:
    """Load evidence store settings from process environment.

    Raises
    ------
    ValueError
        If a required environment variable is missing or cannot be parsed.

    Returns
    -------
    EvidenceStoreSettings
        A frozen settings instance ready for use by :class:`S3EvidenceStore`.
    """

    endpoint_url = os.environ.get(_S3_ENDPOINT_URL, "")
    if not endpoint_url:
        raise ValueError(f"{_S3_ENDPOINT_URL} environment variable is required")

    access_key = os.environ.get(_S3_ACCESS_KEY, "")
    if not access_key:
        raise ValueError(f"{_S3_ACCESS_KEY} environment variable is required")

    secret_key = os.environ.get(_S3_SECRET_KEY, "")
    if not secret_key:
        raise ValueError(f"{_S3_SECRET_KEY} environment variable is required")

    bucket = os.environ.get(_S3_BUCKET, "")
    if not bucket:
        raise ValueError(f"{_S3_BUCKET} environment variable is required")

    region = os.environ.get(_S3_REGION, "auto")

    connect_timeout_str = os.environ.get(_S3_CONNECT_TIMEOUT_SECONDS, "5.0")
    try:
        connect_timeout = float(connect_timeout_str)
    except ValueError:
        raise ValueError(
            f"{_S3_CONNECT_TIMEOUT_SECONDS} must be a float, got {connect_timeout_str!r}"
        )
    if connect_timeout <= 0:
        raise ValueError(
            f"{_S3_CONNECT_TIMEOUT_SECONDS} must be positive, got {connect_timeout}"
        )

    read_timeout_str = os.environ.get(_S3_READ_TIMEOUT_SECONDS, "30.0")
    try:
        read_timeout = float(read_timeout_str)
    except ValueError:
        raise ValueError(
            f"{_S3_READ_TIMEOUT_SECONDS} must be a float, got {read_timeout_str!r}"
        )
    if read_timeout <= 0:
        raise ValueError(
            f"{_S3_READ_TIMEOUT_SECONDS} must be positive, got {read_timeout}"
        )

    return EvidenceStoreSettings(
        endpoint_url=endpoint_url,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        region=region,
        connect_timeout_seconds=connect_timeout,
        read_timeout_seconds=read_timeout,
    )
