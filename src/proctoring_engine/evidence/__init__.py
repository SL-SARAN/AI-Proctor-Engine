"""Evidence & audit store package.

Implements the blob-storage layer for evidence artifacts, as specified
in ``docs/06-evidence-audit-store-design.md``. The package provides:

- **Settings** — environment-loaded S3 configuration.
- **Protocol** — the ``EvidenceStore`` contract for testability.
- **S3 implementation** — production adapter for R2 / MinIO.
- **In-memory store** — test double for unit tests.
- **Storage keys** — deterministic key builder for evidence blobs.
- **Checksum utilities** — SHA-256 integrity verification.
- **Sealing service** — upload-first, row-second evidence persistence.
- **Retention job** — periodic deletion of expired artifacts.

The evidence store is called from the orchestration layer after a
``Flag`` is confirmed. The fusion engine decides *what* to flag;
this layer decides *how* to persist the supporting evidence.

Modules
-------

- :mod:`~proctoring_engine.evidence._settings` — ``EvidenceStoreSettings``
  and ``get_evidence_store_settings``.
- :mod:`~proctoring_engine.evidence._protocol` — ``EvidenceStore`` protocol
  and ``EvidenceStoreError`` / ``EvidenceNotFoundError``.
- :mod:`~proctoring_engine.evidence._s3` — ``S3EvidenceStore`` and
  ``InMemoryEvidenceStore``.
- :mod:`~proctoring_engine.evidence._storage_key` — ``build_storage_key``
  and ``parse_storage_key``.
- :mod:`~proctoring_engine.evidence._checksum` — ``compute_sha256``,
  ``validate_sha256_hex``, ``verify_checksum``.
- :mod:`~proctoring_engine.evidence.service` — ``seal_evidence``,
  ``SealEvidenceRequest``, ``SealEvidenceResult``.
- :mod:`~proctoring_engine.evidence.retention` — ``run_retention_deletion``,
  ``RetentionDeletionResult``.
"""

from __future__ import annotations

# Settings
from proctoring_engine.evidence._settings import (
    EvidenceStoreSettings,
    get_evidence_store_settings,
)

# Protocol
from proctoring_engine.evidence._protocol import (
    EvidenceNotFoundError,
    EvidenceStore,
    EvidenceStoreError,
)

# S3 implementation
from proctoring_engine.evidence._s3 import (
    InMemoryEvidenceStore,
    S3EvidenceStore,
)

# Storage keys
from proctoring_engine.evidence._storage_key import (
    build_storage_key,
    get_artifact_extension,
    parse_storage_key,
)

# Checksum utilities
from proctoring_engine.evidence._checksum import (
    compute_sha256,
    validate_sha256_hex,
    verify_checksum,
)

# Sealing service
from proctoring_engine.evidence.service import (
    EvidenceSealError,
    SealEvidenceRequest,
    SealEvidenceResult,
    seal_evidence,
)

# Retention job
from proctoring_engine.evidence.retention import (
    RetentionDeletionResult,
    run_retention_deletion,
)

__all__ = [
    # Settings
    "EvidenceStoreSettings",
    "get_evidence_store_settings",
    # Protocol
    "EvidenceStore",
    "EvidenceStoreError",
    "EvidenceNotFoundError",
    # S3 implementation
    "S3EvidenceStore",
    "InMemoryEvidenceStore",
    # Storage keys
    "build_storage_key",
    "parse_storage_key",
    "get_artifact_extension",
    # Checksum
    "compute_sha256",
    "validate_sha256_hex",
    "verify_checksum",
    # Service
    "seal_evidence",
    "SealEvidenceRequest",
    "SealEvidenceResult",
    "EvidenceSealError",
    # Retention
    "run_retention_deletion",
    "RetentionDeletionResult",
]
