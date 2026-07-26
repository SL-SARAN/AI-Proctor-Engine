"""SHA-256 checksum utilities for evidence blob integrity.

The evidence store computes a SHA-256 checksum of every uploaded blob
and stores it in ``EvidenceArtifact.content_sha256``. The checksum is
verified on retrieval to detect any silent corruption in the storage
layer.

The implementation uses the standard library's ``hashlib`` — no
external dependencies. The checksum is computed in a single pass for
simplicity; the evidence blobs (short video clips, typically 10–15s)
are small enough that streaming is not required for memory safety.
"""

from __future__ import annotations

import hashlib
from typing import Final


# SHA-256 produces a 32-byte digest, which encodes to 64 hex characters.
_DIGEST_HEX_LENGTH: Final[int] = 64


def compute_sha256(data: bytes) -> str:
    """Compute the SHA-256 checksum of a byte string.

    Parameters
    ----------
    data:
        The blob content.

    Returns
    -------
    str
        Lowercase hex-encoded SHA-256 digest (64 characters).

    Examples
    --------
    >>> compute_sha256(b"hello world")
    'b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9'
    """
    return hashlib.sha256(data).hexdigest()


def validate_sha256_hex(value: str) -> str:
    """Validate that a string is a well-formed SHA-256 hex digest.

    Parameters
    ----------
    value:
        The string to validate.

    Returns
    -------
    str
        The validated string (lowercased).

    Raises
    ------
    ValueError
        If the string is not exactly 64 hex characters.
    """
    if len(value) != _DIGEST_HEX_LENGTH:
        raise ValueError(
            f"SHA-256 hex digest must be exactly 64 characters, got {len(value)}"
        )
    try:
        # Validate hex encoding by attempting to parse
        int(value, 16)
    except ValueError:
        raise ValueError(f"SHA-256 hex digest must be valid hex, got {value!r}")

    return value.lower()


def verify_checksum(data: bytes, expected: str) -> bool:
    """Verify that a blob's checksum matches the expected value.

    Parameters
    ----------
    data:
        The blob content.
    expected:
        The expected SHA-256 hex digest.

    Returns
    -------
    bool
        ``True`` if the computed checksum matches ``expected``.
    """
    return compute_sha256(data) == expected.lower()
