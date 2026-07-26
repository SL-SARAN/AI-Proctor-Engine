"""Evidence store unit tests.

This test suite exercises the evidence store layer without any external
dependencies (no S3 connection, no database). The tests use the
``InMemoryEvidenceStore`` for storage operations and SQLite for ORM
operations where needed.

Test coverage:

- Settings loading and validation
- Checksum computation and validation
- Storage key building and parsing
- EvidenceStore protocol compliance
- InMemoryEvidenceStore operations
- seal_evidence service happy path and error cases
- Retention deletion happy path and edge cases
- Package exports

"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Settings tests
# ---------------------------------------------------------------------------


class TestEvidenceStoreSettings:
    """Tests for EvidenceStoreSettings and get_evidence_store_settings."""

    def test_loads_all_required_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """All required env vars are loaded correctly."""
        monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
        monkeypatch.setenv("S3_ACCESS_KEY", "test-key")
        monkeypatch.setenv("S3_SECRET_KEY", "test-secret")
        monkeypatch.setenv("S3_BUCKET", "test-bucket")
        monkeypatch.setenv("S3_REGION", "us-east-1")

        from proctoring_engine.evidence._settings import get_evidence_store_settings

        settings = get_evidence_store_settings()
        assert settings.endpoint_url == "http://localhost:9000"
        assert settings.access_key == "test-key"
        assert settings.secret_key == "test-secret"
        assert settings.bucket == "test-bucket"
        assert settings.region == "us-east-1"
        assert settings.connect_timeout_seconds == 5.0
        assert settings.read_timeout_seconds == 30.0

    def test_missing_endpoint_url_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing S3_ENDPOINT_URL raises ValueError."""
        monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
        monkeypatch.setenv("S3_ACCESS_KEY", "key")
        monkeypatch.setenv("S3_SECRET_KEY", "secret")
        monkeypatch.setenv("S3_BUCKET", "bucket")

        from proctoring_engine.evidence._settings import get_evidence_store_settings

        with pytest.raises(ValueError, match="S3_ENDPOINT_URL"):
            get_evidence_store_settings()

    def test_missing_access_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing S3_ACCESS_KEY raises ValueError."""
        monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
        monkeypatch.delenv("S3_ACCESS_KEY", raising=False)
        monkeypatch.setenv("S3_SECRET_KEY", "secret")
        monkeypatch.setenv("S3_BUCKET", "bucket")

        from proctoring_engine.evidence._settings import get_evidence_store_settings

        with pytest.raises(ValueError, match="S3_ACCESS_KEY"):
            get_evidence_store_settings()

    def test_missing_secret_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing S3_SECRET_KEY raises ValueError."""
        monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
        monkeypatch.setenv("S3_ACCESS_KEY", "key")
        monkeypatch.delenv("S3_SECRET_KEY", raising=False)
        monkeypatch.setenv("S3_BUCKET", "bucket")

        from proctoring_engine.evidence._settings import get_evidence_store_settings

        with pytest.raises(ValueError, match="S3_SECRET_KEY"):
            get_evidence_store_settings()

    def test_missing_bucket_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Missing S3_BUCKET raises ValueError."""
        monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
        monkeypatch.setenv("S3_ACCESS_KEY", "key")
        monkeypatch.setenv("S3_SECRET_KEY", "secret")
        monkeypatch.delenv("S3_BUCKET", raising=False)

        from proctoring_engine.evidence._settings import get_evidence_store_settings

        with pytest.raises(ValueError, match="S3_BUCKET"):
            get_evidence_store_settings()

    def test_default_region_is_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Default region is 'auto' for R2 compatibility."""
        monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
        monkeypatch.setenv("S3_ACCESS_KEY", "key")
        monkeypatch.setenv("S3_SECRET_KEY", "secret")
        monkeypatch.setenv("S3_BUCKET", "bucket")
        monkeypatch.delenv("S3_REGION", raising=False)

        from proctoring_engine.evidence._settings import get_evidence_store_settings

        settings = get_evidence_store_settings()
        assert settings.region == "auto"

    def test_custom_timeouts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Custom timeout values are parsed correctly."""
        monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
        monkeypatch.setenv("S3_ACCESS_KEY", "key")
        monkeypatch.setenv("S3_SECRET_KEY", "secret")
        monkeypatch.setenv("S3_BUCKET", "bucket")
        monkeypatch.setenv("S3_CONNECT_TIMEOUT_SECONDS", "10.5")
        monkeypatch.setenv("S3_READ_TIMEOUT_SECONDS", "60")

        from proctoring_engine.evidence._settings import get_evidence_store_settings

        settings = get_evidence_store_settings()
        assert settings.connect_timeout_seconds == 10.5
        assert settings.read_timeout_seconds == 60.0

    def test_invalid_connect_timeout_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Invalid connect timeout raises ValueError."""
        monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
        monkeypatch.setenv("S3_ACCESS_KEY", "key")
        monkeypatch.setenv("S3_SECRET_KEY", "secret")
        monkeypatch.setenv("S3_BUCKET", "bucket")
        monkeypatch.setenv("S3_CONNECT_TIMEOUT_SECONDS", "not-a-number")

        from proctoring_engine.evidence._settings import get_evidence_store_settings

        with pytest.raises(ValueError, match="must be a float"):
            get_evidence_store_settings()

    def test_negative_connect_timeout_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative connect timeout raises ValueError."""
        monkeypatch.setenv("S3_ENDPOINT_URL", "http://localhost:9000")
        monkeypatch.setenv("S3_ACCESS_KEY", "key")
        monkeypatch.setenv("S3_SECRET_KEY", "secret")
        monkeypatch.setenv("S3_BUCKET", "bucket")
        monkeypatch.setenv("S3_CONNECT_TIMEOUT_SECONDS", "-5")

        from proctoring_engine.evidence._settings import get_evidence_store_settings

        with pytest.raises(ValueError, match="must be positive"):
            get_evidence_store_settings()


# ---------------------------------------------------------------------------
# Checksum tests
# ---------------------------------------------------------------------------


class TestChecksum:
    """Tests for SHA-256 checksum utilities."""

    def test_compute_sha256_known_value(self) -> None:
        """compute_sha256 returns correct hash for known input."""
        from proctoring_engine.evidence._checksum import compute_sha256

        # "hello world" SHA-256 is a known value
        result = compute_sha256(b"hello world")
        assert result == (
            "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        )

    def test_compute_sha256_empty_input(self) -> None:
        """compute_sha256 handles empty input."""
        from proctoring_engine.evidence._checksum import compute_sha256

        # Empty string SHA-256
        result = compute_sha256(b"")
        assert result == (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        )

    def test_compute_sha256_returns_lowercase(self) -> None:
        """compute_sha256 always returns lowercase hex."""
        from proctoring_engine.evidence._checksum import compute_sha256

        result = compute_sha256(b"test")
        assert result == result.lower()
        assert len(result) == 64

    def test_validate_sha256_hex_accepts_valid(self) -> None:
        """validate_sha256_hex accepts valid 64-char hex strings."""
        from proctoring_engine.evidence._checksum import validate_sha256_hex

        valid = "a" * 64
        result = validate_sha256_hex(valid)
        assert result == valid

    def test_validate_sha256_hex_rejects_wrong_length(self) -> None:
        """validate_sha256_hex rejects strings not 64 chars."""
        from proctoring_engine.evidence._checksum import validate_sha256_hex

        with pytest.raises(ValueError, match="exactly 64 characters"):
            validate_sha256_hex("a" * 63)

        with pytest.raises(ValueError, match="exactly 64 characters"):
            validate_sha256_hex("a" * 65)

    def test_validate_sha256_hex_rejects_non_hex(self) -> None:
        """validate_sha256_hex rejects non-hex characters."""
        from proctoring_engine.evidence._checksum import validate_sha256_hex

        with pytest.raises(ValueError, match="valid hex"):
            validate_sha256_hex("g" * 64)  # 'g' is not hex

    def test_verify_checksum_matches(self) -> None:
        """verify_checksum returns True for matching checksum."""
        from proctoring_engine.evidence._checksum import (
            compute_sha256,
            verify_checksum,
        )

        data = b"test data"
        checksum = compute_sha256(data)
        assert verify_checksum(data, checksum) is True

    def test_verify_checksum_mismatch(self) -> None:
        """verify_checksum returns False for mismatched checksum."""
        from proctoring_engine.evidence._checksum import verify_checksum

        data = b"test data"
        wrong_checksum = "a" * 64
        assert verify_checksum(data, wrong_checksum) is False

    def test_verify_checksum_case_insensitive(self) -> None:
        """verify_checksum is case-insensitive for expected value."""
        from proctoring_engine.evidence._checksum import (
            compute_sha256,
            verify_checksum,
        )

        data = b"test data"
        checksum = compute_sha256(data)
        assert verify_checksum(data, checksum.upper()) is True


# ---------------------------------------------------------------------------
# Storage key tests
# ---------------------------------------------------------------------------


class TestStorageKey:
    """Tests for storage key building and parsing."""

    def test_build_storage_key_clip(self) -> None:
        """build_storage_key produces correct key for clip artifact."""
        from proctoring_engine.evidence._storage_key import build_storage_key

        session_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        flag_id = uuid.UUID("00000000-0000-0000-0000-000000000002")

        key = build_storage_key(session_id, flag_id, "clip")

        assert key == (
            "evidence/00000000-0000-0000-0000-000000000001/"
            "00000000-0000-0000-0000-000000000002/clip.webm"
        )

    def test_build_storage_key_frame(self) -> None:
        """build_storage_key produces correct key for frame artifact."""
        from proctoring_engine.evidence._storage_key import build_storage_key

        session_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        flag_id = uuid.UUID("00000000-0000-0000-0000-000000000002")

        key = build_storage_key(session_id, flag_id, "frame")

        assert key.endswith("frame.jpg")

    def test_build_storage_key_audio(self) -> None:
        """build_storage_key produces correct key for audio artifact."""
        from proctoring_engine.evidence._storage_key import build_storage_key

        key = build_storage_key(uuid.uuid4(), uuid.uuid4(), "audio")
        assert key.endswith("audio.webm")

    def test_build_storage_key_event_export(self) -> None:
        """build_storage_key produces correct key for event_export artifact."""
        from proctoring_engine.evidence._storage_key import build_storage_key

        key = build_storage_key(uuid.uuid4(), uuid.uuid4(), "event_export")
        assert key.endswith("event_export.json")

    def test_build_storage_key_invalid_type_raises(self) -> None:
        """build_storage_key raises for unknown artifact type."""
        from proctoring_engine.evidence._storage_key import build_storage_key

        with pytest.raises(ValueError, match="Unknown artifact_type"):
            build_storage_key(uuid.uuid4(), uuid.uuid4(), "unknown")

    def test_parse_storage_key_valid(self) -> None:
        """parse_storage_key extracts components from valid key."""
        from proctoring_engine.evidence._storage_key import parse_storage_key

        session_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        flag_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
        key = f"evidence/{session_id}/{flag_id}/clip.webm"

        parsed_session, parsed_flag, artifact_type = parse_storage_key(key)

        assert parsed_session == session_id
        assert parsed_flag == flag_id
        assert artifact_type == "clip"

    def test_parse_storage_key_invalid_format_raises(self) -> None:
        """parse_storage_key raises for malformed key."""
        from proctoring_engine.evidence._storage_key import parse_storage_key

        with pytest.raises(ValueError, match="4 parts"):
            parse_storage_key("invalid/key")

    def test_parse_storage_key_missing_evidence_prefix_raises(self) -> None:
        """parse_storage_key raises if key doesn't start with evidence/."""
        from proctoring_engine.evidence._storage_key import parse_storage_key

        with pytest.raises(ValueError, match="must start with 'evidence/'"):
            parse_storage_key("other/session/flag/clip.webm")

    def test_parse_storage_key_invalid_session_id_raises(self) -> None:
        """parse_storage_key raises for invalid session UUID."""
        from proctoring_engine.evidence._storage_key import parse_storage_key

        with pytest.raises(ValueError, match="Invalid exam_session_id"):
            parse_storage_key("evidence/not-a-uuid/flag/clip.webm")

    def test_parse_storage_key_invalid_flag_id_raises(self) -> None:
        """parse_storage_key raises for invalid flag UUID."""
        from proctoring_engine.evidence._storage_key import parse_storage_key

        session_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        with pytest.raises(ValueError, match="Invalid flag_id"):
            parse_storage_key(f"evidence/{session_id}/not-a-uuid/clip.webm")

    def test_parse_storage_key_extension_mismatch_raises(self) -> None:
        """parse_storage_key raises if extension doesn't match type."""
        from proctoring_engine.evidence._storage_key import parse_storage_key

        session_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
        flag_id = uuid.UUID("00000000-0000-0000-0000-000000000002")

        with pytest.raises(ValueError, match="Extension mismatch"):
            parse_storage_key(f"evidence/{session_id}/{flag_id}/clip.wrong")

    def test_get_artifact_extension_valid(self) -> None:
        """get_artifact_extension returns correct extension."""
        from proctoring_engine.evidence._storage_key import get_artifact_extension

        assert get_artifact_extension("frame") == "jpg"
        assert get_artifact_extension("clip") == "webm"
        assert get_artifact_extension("audio") == "webm"
        assert get_artifact_extension("event_export") == "json"

    def test_get_artifact_extension_invalid_raises(self) -> None:
        """get_artifact_extension raises for unknown type."""
        from proctoring_engine.evidence._storage_key import get_artifact_extension

        with pytest.raises(ValueError, match="Unknown artifact_type"):
            get_artifact_extension("unknown")


# ---------------------------------------------------------------------------
# InMemoryEvidenceStore tests
# ---------------------------------------------------------------------------


class TestInMemoryEvidenceStore:
    """Tests for the in-memory evidence store test double."""

    @pytest.fixture
    def store(self) -> Any:
        from proctoring_engine.evidence._s3 import InMemoryEvidenceStore

        return InMemoryEvidenceStore()

    def test_upload_and_download(self, store: Any) -> None:
        """Upload followed by download returns same data."""
        key = "evidence/test/test/clip.webm"
        data = b"test video data"

        store.upload(key, data)
        result = store.download(key)

        assert result == data

    def test_download_nonexistent_raises(self, store: Any) -> None:
        """Download of nonexistent key raises EvidenceNotFoundError."""
        from proctoring_engine.evidence._protocol import EvidenceNotFoundError

        with pytest.raises(EvidenceNotFoundError):
            store.download("nonexistent/key")

    def test_exists_returns_true_for_existing(self, store: Any) -> None:
        """exists returns True for uploaded key."""
        key = "evidence/test/test/clip.webm"
        store.upload(key, b"data")

        assert store.exists(key) is True

    def test_exists_returns_false_for_nonexistent(self, store: Any) -> None:
        """exists returns False for nonexistent key."""
        assert store.exists("nonexistent/key") is False

    def test_delete_removes_key(self, store: Any) -> None:
        """Delete removes the key."""
        key = "evidence/test/test/clip.webm"
        store.upload(key, b"data")
        store.delete(key)

        assert store.exists(key) is False

    def test_delete_nonexistent_is_idempotent(self, store: Any) -> None:
        """Delete of nonexistent key succeeds silently."""
        store.delete("nonexistent/key")  # Should not raise

    def test_compute_checksum(self, store: Any) -> None:
        """compute_checksum returns correct SHA-256."""
        from proctoring_engine.evidence._checksum import compute_sha256

        key = "evidence/test/test/clip.webm"
        data = b"test data"
        store.upload(key, data)

        result = store.compute_checksum(key)
        expected = compute_sha256(data)

        assert result == expected

    def test_overwrite_on_upload(self, store: Any) -> None:
        """Upload to same key overwrites previous data."""
        key = "evidence/test/test/clip.webm"
        store.upload(key, b"original")
        store.upload(key, b"replacement")

        assert store.download(key) == b"replacement"

    def test_list_keys(self, store: Any) -> None:
        """list_keys returns all stored keys."""
        store.upload("key1", b"a")
        store.upload("key2", b"b")

        keys = store.list_keys()
        assert set(keys) == {"key1", "key2"}

    def test_get_blob_count(self, store: Any) -> None:
        """get_blob_count returns number of stored blobs."""
        assert store.get_blob_count() == 0
        store.upload("key1", b"a")
        assert store.get_blob_count() == 1
        store.upload("key2", b"b")
        assert store.get_blob_count() == 2
        store.delete("key1")
        assert store.get_blob_count() == 1


# ---------------------------------------------------------------------------
# Sealing service tests
# ---------------------------------------------------------------------------


class TestSealEvidenceService:
    """Tests for the seal_evidence service function."""

    @pytest.fixture
    def store(self) -> Any:
        from proctoring_engine.evidence._s3 import InMemoryEvidenceStore
        return InMemoryEvidenceStore()

    @pytest.fixture
    def request_data(self) -> Any:
        from proctoring_engine.evidence.service import SealEvidenceRequest

        now = datetime.now(timezone.utc)
        return SealEvidenceRequest(
            flag_id=uuid.uuid4(),
            exam_session_id=uuid.uuid4(),
            artifact_type="clip",
            media_type="video/webm",
            blob=b"raw video data",
            capture_started_at=now - timedelta(seconds=10),
            capture_ended_at=now,
            retention_expires_at=now + timedelta(days=90),
        )

    def test_happy_path(self, store: Any, request_data: Any) -> None:
        """seal_evidence uploads blob, checksums, and returns correct result."""
        from proctoring_engine.evidence.service import seal_evidence
        from proctoring_engine.evidence._checksum import compute_sha256

        result = seal_evidence(store, request_data)

        # Blob was uploaded
        assert store.exists(result.storage_key)
        assert store.download(result.storage_key) == request_data.blob

        # Checksum is correct
        assert result.content_sha256 == compute_sha256(request_data.blob)

        # Metadata matches
        assert result.byte_size == len(request_data.blob)
        assert result.media_type == request_data.media_type
        assert result.capture_started_at == request_data.capture_started_at
        assert result.retention_expires_at == request_data.retention_expires_at

    def test_to_orm_kwargs(self, store: Any, request_data: Any) -> None:
        """to_orm_kwargs prepares dict for EvidenceArtifact ORM model."""
        from proctoring_engine.evidence.service import seal_evidence

        result = seal_evidence(store, request_data)
        kwargs = result.to_orm_kwargs(flag_id=request_data.flag_id)

        assert kwargs["flag_id"] == request_data.flag_id
        assert kwargs["kind"] == "clip"
        assert kwargs["storage_uri"] == f"s3://{result.storage_key}"
        assert kwargs["content_sha256"] == result.content_sha256
        assert kwargs["media_type"] == request_data.media_type
        assert kwargs["byte_size"] == len(request_data.blob)
        assert kwargs["retention_expires_at"] == request_data.retention_expires_at

    def test_invalid_artifact_type_raises(
        self, store: Any, request_data: Any
    ) -> None:
        """Invalid artifact_type raises EvidenceSealError before upload."""
        from proctoring_engine.evidence.service import (
            seal_evidence,
            EvidenceSealError,
        )
        from dataclasses import replace

        bad_request = replace(request_data, artifact_type="unknown")

        with pytest.raises(EvidenceSealError, match="Invalid artifact_type"):
            seal_evidence(store, bad_request)

        # Nothing was uploaded
        assert store.get_blob_count() == 0

    def test_upload_failure_raises(
        self, store: Any, request_data: Any
    ) -> None:
        """Upload error raises EvidenceSealError."""
        from proctoring_engine.evidence.service import seal_evidence, EvidenceSealError
        from proctoring_engine.evidence._protocol import EvidenceStoreError

        def mock_upload(*args, **kwargs):
            raise EvidenceStoreError("Upload failed")

        store.upload = mock_upload  # type: ignore[method-assign]

        with pytest.raises(EvidenceSealError, match="Failed to upload evidence"):
            seal_evidence(store, request_data)

    def test_checksum_mismatch_raises_and_deletes_blob(
        self, store: Any, request_data: Any
    ) -> None:
        """If remote checksum differs from local, deletes blob and raises."""
        from proctoring_engine.evidence.service import seal_evidence, EvidenceSealError
        import types

        def mock_compute_checksum(self, key):
            return "0" * 64  # Wrong checksum

        store.compute_checksum = types.MethodType(mock_compute_checksum, store)

        with pytest.raises(EvidenceSealError, match="Checksum mismatch after upload"):
            seal_evidence(store, request_data)

        # The corrupted blob should have been deleted
        assert store.get_blob_count() == 0

    def test_frame_artifact_type(self, store: Any, request_data: Any) -> None:
        """Frame artifact type produces .jpg extension."""
        from proctoring_engine.evidence.service import seal_evidence
        from dataclasses import replace

        frame_request = replace(request_data, artifact_type="frame")
        result = seal_evidence(store, frame_request)

        assert result.storage_key.endswith("frame.jpg")
        assert store.exists(result.storage_key)

    def test_event_export_artifact_type(self, store: Any, request_data: Any) -> None:
        """Event export artifact type produces .json extension."""
        from proctoring_engine.evidence.service import seal_evidence
        from dataclasses import replace

        json_request = replace(
            request_data,
            artifact_type="event_export",
            media_type="application/json",
            blob=b'{"events": []}',
        )
        result = seal_evidence(store, json_request)

        assert result.storage_key.endswith("event_export.json")
        assert store.exists(result.storage_key)


# ---------------------------------------------------------------------------
# Retention deletion tests
# ---------------------------------------------------------------------------


class TestRetentionDeletionWorker:
    """Tests for the run_retention_deletion job."""

    @pytest.fixture
    def store(self) -> Any:
        from proctoring_engine.evidence._s3 import InMemoryEvidenceStore
        return InMemoryEvidenceStore()

    @pytest.fixture
    def db_session(self) -> Any:
        """A real SQLite in-memory session with the schema created."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from proctoring_engine.models import Base

        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        return Session(engine)

    def _create_artifact(
        self,
        db: Any,
        retention_expires_at: datetime,
        store: Any,
    ) -> tuple[uuid.UUID, str]:
        """Create a participant, policy, session, flag, and artifact.

        Returns the artifact id and storage key.
        """
        from proctoring_engine.models import (
            Participant,
            ExamSession,
            PolicyConfig,
            Flag,
            EvidenceArtifact,
        )

        now = datetime.now(timezone.utc)
        unique_id = uuid.uuid4()
        participant = Participant(
            lti_issuer=f"test_issuer_{unique_id}",
            lms_user_reference=f"user_{unique_id}",
            consent_recorded_at=now - timedelta(days=10),
        )
        policy = PolicyConfig(name=f"test_policy_{unique_id}")
        db.add(participant)
        db.add(policy)
        db.flush()

        session = ExamSession(
            participant_id=participant.id,
            policy_config_id=policy.id,
            lti_issuer=participant.lti_issuer,
            lti_context_id=f"ctx_{unique_id}",
            exam_reference=f"exam_{unique_id}",
            attempt_reference=str(uuid.uuid4()),
            consent_recorded_at=now - timedelta(days=10),
        )
        db.add(session)
        db.flush()

        flag = Flag(
            exam_session_id=session.id,
            policy_config_id=policy.id,
            rule_code="second_person",
            severity="critical",
            confidence_score=0.9,
            confidence_lower=0.8,
            confidence_upper=0.95,
        )
        db.add(flag)
        db.flush()

        storage_key = (
            f"evidence/{session.id}/{flag.id}/clip.webm"
        )
        store.upload(storage_key, b"blob content")

        artifact = EvidenceArtifact(
            flag_id=flag.id,
            kind="clip",
            storage_uri=f"s3://{storage_key}",
            content_sha256="0" * 64,
            media_type="video/webm",
            byte_size=12,
            capture_started_at=now - timedelta(days=5),
            retention_expires_at=retention_expires_at,
        )
        db.add(artifact)
        db.commit()
        return artifact.id, storage_key

    def test_deletes_expired_artifact(
        self, db_session: Any, store: Any
    ) -> None:
        """Expired artifact is deleted: blob + row."""
        from proctoring_engine.evidence.retention import run_retention_deletion
        from proctoring_engine.models import EvidenceArtifact

        now = datetime.now(timezone.utc)
        artifact_id, key = self._create_artifact(
            db_session,
            retention_expires_at=now - timedelta(hours=1),
            store=store,
        )
        assert store.exists(key)

        result = run_retention_deletion(db_session, store, now=now)

        assert result.artifacts_deleted == 1
        assert result.storage_errors == 0
        assert not store.exists(key)
        # Row is gone
        assert (
            db_session.query(EvidenceArtifact)
            .filter(EvidenceArtifact.id == artifact_id)
            .one_or_none()
            is None
        )

    def test_leaves_unexpired_artifact(
        self, db_session: Any, store: Any
    ) -> None:
        """Unexpired artifact is left alone."""
        from proctoring_engine.evidence.retention import run_retention_deletion
        from proctoring_engine.models import EvidenceArtifact

        now = datetime.now(timezone.utc)
        artifact_id, key = self._create_artifact(
            db_session,
            retention_expires_at=now + timedelta(hours=1),
            store=store,
        )
        assert store.exists(key)

        result = run_retention_deletion(db_session, store, now=now)

        assert result.artifacts_deleted == 0
        assert store.exists(key)
        # Row is still there
        assert (
            db_session.query(EvidenceArtifact)
            .filter(EvidenceArtifact.id == artifact_id)
            .one_or_none()
            is not None
        )

    def test_deletes_multiple_expired(
        self, db_session: Any, store: Any
    ) -> None:
        """Multiple expired artifacts are all deleted."""
        from proctoring_engine.evidence.retention import run_retention_deletion

        now = datetime.now(timezone.utc)
        ids_and_keys = [
            self._create_artifact(
                db_session,
                retention_expires_at=now - timedelta(hours=1),
                store=store,
            )
            for _ in range(3)
        ]

        result = run_retention_deletion(db_session, store, now=now)

        assert result.artifacts_deleted == 3
        for _, key in ids_and_keys:
            assert not store.exists(key)

    def test_empty_database_returns_zero(
        self, db_session: Any, store: Any
    ) -> None:
        """Empty database returns zero counts."""
        from proctoring_engine.evidence.retention import run_retention_deletion

        now = datetime.now(timezone.utc)
        result = run_retention_deletion(db_session, store, now=now)

        assert result.artifacts_deleted == 0
        assert result.storage_errors == 0

    def test_blob_already_missing_still_deletes_row(
        self, db_session: Any, store: Any
    ) -> None:
        """If blob is already gone, row is still deleted (idempotent)."""
        from proctoring_engine.evidence.retention import run_retention_deletion
        from proctoring_engine.models import EvidenceArtifact

        now = datetime.now(timezone.utc)
        artifact_id, key = self._create_artifact(
            db_session,
            retention_expires_at=now - timedelta(hours=1),
            store=store,
        )
        # Pre-delete the blob to simulate prior deletion
        store.delete(key)
        assert not store.exists(key)

        result = run_retention_deletion(db_session, store, now=now)

        assert result.artifacts_deleted == 1
        assert result.storage_errors == 0
        assert (
            db_session.query(EvidenceArtifact)
            .filter(EvidenceArtifact.id == artifact_id)
            .one_or_none()
            is None
        )

    def test_storage_error_leaves_row(
        self, db_session: Any, store: Any
    ) -> None:
        """If blob deletion fails, row is left in place."""
        from proctoring_engine.evidence.retention import run_retention_deletion
        from proctoring_engine.evidence._protocol import EvidenceStoreError
        from proctoring_engine.models import EvidenceArtifact

        now = datetime.now(timezone.utc)
        artifact_id, key = self._create_artifact(
            db_session,
            retention_expires_at=now - timedelta(hours=1),
            store=store,
        )

        def failing_delete(k):
            raise EvidenceStoreError("simulated storage failure")

        store.delete = failing_delete  # type: ignore[method-assign]

        result = run_retention_deletion(db_session, store, now=now)

        assert result.artifacts_deleted == 0
        assert result.storage_errors == 1
        # Row is still there
        assert (
            db_session.query(EvidenceArtifact)
            .filter(EvidenceArtifact.id == artifact_id)
            .one_or_none()
            is not None
        )


# ---------------------------------------------------------------------------
# Protocol compliance tests
# ---------------------------------------------------------------------------


class TestEvidenceStoreProtocol:
    """Verify InMemoryEvidenceStore satisfies the EvidenceStore protocol."""

    def test_in_memory_satisfies_protocol(self) -> None:
        """InMemoryEvidenceStore is a runtime instance of EvidenceStore."""
        from proctoring_engine.evidence._s3 import InMemoryEvidenceStore
        from proctoring_engine.evidence._protocol import EvidenceStore

        store = InMemoryEvidenceStore()
        assert isinstance(store, EvidenceStore)

    def test_s3_store_has_protocol_methods(self) -> None:
        """S3EvidenceStore class defines the protocol's methods."""
        from proctoring_engine.evidence._s3 import S3EvidenceStore

        for method_name in (
            "upload",
            "download",
            "delete",
            "exists",
            "compute_checksum",
        ):
            assert hasattr(S3EvidenceStore, method_name), (
                f"S3EvidenceStore missing method {method_name}"
            )


# ---------------------------------------------------------------------------
# Package exports tests
# ---------------------------------------------------------------------------


class TestPackageExports:
    """Verify the evidence package exports the expected symbols."""

    def test_all_exports_importable(self) -> None:
        """All symbols in __all__ are importable from the package."""
        import proctoring_engine.evidence as pkg

        for name in pkg.__all__:
            assert hasattr(pkg, name), f"Missing export: {name}"

    def test_specific_exports_exist(self) -> None:
        """Specific expected symbols are exported."""
        from proctoring_engine.evidence import (
            EvidenceStoreSettings,
            get_evidence_store_settings,
            EvidenceStore,
            EvidenceStoreError,
            EvidenceNotFoundError,
            S3EvidenceStore,
            InMemoryEvidenceStore,
            build_storage_key,
            parse_storage_key,
            get_artifact_extension,
            compute_sha256,
            validate_sha256_hex,
            verify_checksum,
            seal_evidence,
            SealEvidenceRequest,
            SealEvidenceResult,
            EvidenceSealError,
            run_retention_deletion,
            RetentionDeletionResult,
        )

        assert EvidenceStoreSettings is not None
        assert S3EvidenceStore is not None
        assert InMemoryEvidenceStore is not None
