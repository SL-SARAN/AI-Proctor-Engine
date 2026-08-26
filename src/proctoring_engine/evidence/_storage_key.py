"""Storage key builder for evidence blobs.

The key structure follows the design in ``docs/06-evidence-audit-store-design.md``:

    evidence/{exam_session_id}/{flag_id}/{artifact_type}.{ext}

This predictable hierarchy allows:
- Direct lookup without a database round-trip (given session + flag).
- Bucket lifecycle rules scoped to a session prefix.
- Easy auditing of storage layout.

The key builder is a pure function with no side effects.
"""

from __future__ import annotations

import uuid
from typing import Final


# Supported artifact types and their canonical file extensions.
# The extension is lowercased and excludes the leading dot.
_ARTIFACT_EXTENSIONS: Final[dict[str, str]] = {
    "frame": "jpg",
    "clip": "webm",
    "audio": "webm",
    "event_export": "json",
}


def build_storage_key(
    exam_session_id: uuid.UUID,
    flag_id: uuid.UUID,
    artifact_type: str,
) -> str:
    """Build the storage key for an evidence blob.

    Parameters
    ----------
    exam_session_id:
        The ``ExamSession.id`` this evidence belongs to.
    flag_id:
        The ``Flag.id`` that triggered this evidence capture.
    artifact_type:
        One of ``frame``, ``clip``, ``audio``, ``event_export``.
        Determines the file extension.

    Returns
    -------
    str
        The storage key: ``evidence/{session_id}/{flag_id}/{type}.{ext}``.

    Raises
    ------
    ValueError
        If ``artifact_type`` is not recognised.

    Examples
    --------
    >>> from uuid import UUID
    >>> build_storage_key(
    ...     UUID("00000000-0000-0000-0000-000000000001"),
    ...     UUID("00000000-0000-0000-0000-000000000002"),
    ...     "clip",
    ... )
    'evidence/00000000-0000-0000-0000-000000000001/00000000-0000-0000-0000-000000000002/clip.webm'
    """
    ext = _ARTIFACT_EXTENSIONS.get(artifact_type)
    if ext is None:
        raise ValueError(
            f"Unknown artifact_type {artifact_type!r}; "
            f"expected one of {sorted(_ARTIFACT_EXTENSIONS.keys())}"
        )

    # Use the short UUID format (no braces, lowercase hex)
    session_str = str(exam_session_id)
    flag_str = str(flag_id)

    return f"evidence/{session_str}/{flag_str}/{artifact_type}.{ext}"


def parse_storage_key(key: str) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Parse a storage key into its components.

    Parameters
    ----------
    key:
        The storage key.

    Returns
    -------
    tuple[UUID, UUID, str]
        ``(exam_session_id, flag_id, artifact_type)``.

    Raises
    ------
    ValueError
        If the key does not match the expected format.

    Examples
    --------
    >>> parse_storage_key(
    ...     "evidence/00000000-0000-0000-0000-000000000001/"
    ...     "00000000-0000-0000-0000-000000000002/clip.webm"
    ... )
    (UUID('00000000-0000-0000-0000-000000000001'), UUID('00000000-0000-0000-0000-000000000002'), 'clip')
    """
    parts = key.split("/")
    if len(parts) != 4:
        raise ValueError(
            f"Storage key must have 4 parts separated by '/', got {len(parts)}: {key!r}"
        )
    if parts[0] != "evidence":
        raise ValueError(f"Storage key must start with 'evidence/', got {parts[0]!r}")

    try:
        session_id = uuid.UUID(parts[1])
    except ValueError:
        raise ValueError(f"Invalid exam_session_id in key: {parts[1]!r}")

    try:
        flag_id = uuid.UUID(parts[2])
    except ValueError:
        raise ValueError(f"Invalid flag_id in key: {parts[2]!r}")

    # Extract artifact_type from "type.ext"
    filename = parts[3]
    if "." not in filename:
        raise ValueError(f"Filename must contain a dot: {filename!r}")

    artifact_type, ext = filename.rsplit(".", 1)
    expected_ext = _ARTIFACT_EXTENSIONS.get(artifact_type)
    if expected_ext is None:
        raise ValueError(f"Unknown artifact_type in filename: {artifact_type!r}")
    if ext != expected_ext:
        raise ValueError(
            f"Extension mismatch for {artifact_type}: expected {expected_ext!r}, got {ext!r}"
        )

    return session_id, flag_id, artifact_type


def get_artifact_extension(artifact_type: str) -> str:
    """Get the canonical file extension for an artifact type.

    Parameters
    ----------
    artifact_type:
        One of ``frame``, ``clip``, ``audio``, ``event_export``.

    Returns
    -------
    str
        The file extension (without leading dot).

    Raises
    ------
    ValueError
        If ``artifact_type`` is not recognised.
    """
    ext = _ARTIFACT_EXTENSIONS.get(artifact_type)
    if ext is None:
        raise ValueError(
            f"Unknown artifact_type {artifact_type!r}; "
            f"expected one of {sorted(_ARTIFACT_EXTENSIONS.keys())}"
        )
    return ext
