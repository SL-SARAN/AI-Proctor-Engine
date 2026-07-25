"""Client rolling-buffer contract (server-side).

The rolling buffer is conceptually a *browser-side* thing — it
exists in the exam-client JavaScript layer.  See
``docs/03-preprocessing-layer-design.md`` §3 and
``docs/proctoring-engine-v1-spec.md`` §"Evidence retention".

This module is the **server-side contract** for the buffer:

- The :class:`RollingBuffer` protocol documents the *shape* a
  flushed buffer must have.  The actual capture happens on the
  client — this Python module exists so server-side code can rely
  on the same data shape and so verification / unit tests have a
  well-defined in-Python object to walk over.
- The :class:`NullRollingBuffer` is a no-op implementation that
  satisfies the same shape — useful in tests, and for diagnostic
  path that queries buffer state without recording entries.

**The hard rule** (enforced everywhere in the system, repeated for
emphasis): the client rolling buffer is **never** transmitted
during normal operation.  Flushes happen *only* on a confirmed
flag, gated by the kill-switch path.  The frame-upload route
(added in ``docs/05``) is the only consumer of a flushed buffer.

The buffer model includes the **outer time-window** of the flush:
``captured_window_start`` and ``captured_window_end`` of the
triggered :class:`EvidenceArtifact`, derived from the flush's
actual frame timestamps — *not* from the wall-clock "now."
"""

from __future__ import annotations

import base64
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class BufferFlushError(Exception):
    """Raised when a buffer-flush envelope is malformed."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RollingBufferConfig:
    """Tunables the *client* would use to size its circular buffer.

    These are also the bounds the server validates a flushed buffer
    against — a 100 MB flush is rejected.

    Per design doc §3: 200-500 ms capture interval, last 10-15 s.
    Sized for that:

    - ``capture_interval_ms`` ∊ [200, 500]
    - ``window_seconds`` ∊ [10, 15]
    - ``max_entries`` is derived: ``window / interval``.
    - ``max_bytes_per_entry`` is bounded so a single frame can't
      bloat the upload.
    - ``max_total_bytes`` is the upper bound for a flushed buffer.
    """

    capture_interval_ms: int = 300
    window_seconds: int = 15
    max_bytes_per_entry: int = 1_500_000   # ~1.44 MiB / frame
    max_entries: int = 60                 # 15 s @ 250 ms = 60
    max_total_bytes: int = 60 * 1_500_000

    def __post_init__(self) -> None:
        if not (200 <= self.capture_interval_ms <= 500):
            raise ValueError(
                f"capture_interval_ms={self.capture_interval_ms} is outside "
                f"the spec window [200, 500]."
            )
        if not (10 <= self.window_seconds <= 15):
            raise ValueError(
                f"window_seconds={self.window_seconds} is outside the spec "
                f"window [10, 15]."
            )
        if self.max_entries <= 0:
            raise ValueError("max_entries must be a positive integer.")
        if self.max_bytes_per_entry <= 0:
            raise ValueError("max_bytes_per_entry must be a positive integer.")
        if self.max_total_bytes <= 0:
            raise ValueError("max_total_bytes must be a positive integer.")


# ---------------------------------------------------------------------------
# Buffer entries
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RollingBufferEntry:
    """A single frame stored in the rolling buffer.

    Carrying the *base64-encoded* bytes here — we're modelling the
    on-the-wire shape.  Decoding is the job of the preprocessing
    layer's frame module; the buffer doesn't decode eagerly.
    """

    captured_at: datetime
    encoded: str  # base64
    encoding: str  # 'jpeg', 'png', 'webp'
    resolution: tuple[int, int]  # (width, height)
    size_bytes: int

    def __post_init__(self) -> None:
        if self.size_bytes < 0:
            raise BufferFlushError(
                f"entry size_bytes={self.size_bytes} cannot be negative."
            )
        if self.encoding not in ("jpeg", "png", "webp"):
            raise BufferFlushError(
                f"entry encoding='{self.encoding}' is not supported."
            )
        if not self.encoded:
            raise BufferFlushError("entry encoded payload is empty.")
        if len(self.resolution) != 2:
            raise BufferFlushError("resolution must be a (width, height) tuple.")


# ---------------------------------------------------------------------------
# Buffer protocol
# ---------------------------------------------------------------------------

@runtime_checkable
class RollingBuffer(Protocol):
    """The shape the rolling buffer satisfies on both sides of the wire.

    The client maintains a ``RollingBuffer`` in JavaScript during the
    session.  When a flag is confirmed and the server pushes a
    ``kill_switch`` down, the client invokes
    :meth:`flush_to_server`, transporting the buffer's current
    contents to the evidence-store layer.

    On the server side, an upload handler validates the received
    flush against :class:`RollingBufferConfig` and persists the
    buffer as a single :class:`EvidenceArtifact`.
    """

    config: RollingBufferConfig
    session_id: str

    def __len__(self) -> int: ...
    def __iter__(self) -> Iterator[RollingBufferEntry]: ...

    def window_start(self) -> datetime | None:
        """The captured_at of the oldest entry, or None if empty."""

    def window_end(self) -> datetime | None:
        """The captured_at of the newest entry, or None if empty."""

    def total_bytes(self) -> int:
        """Total bytes of encoded payload currently in the buffer."""

    def to_serializable(self) -> dict:
        """Return a JSON-serialisable shape for transport upload."""


# ---------------------------------------------------------------------------
# In-Python implementation (server-side test / diagnostic use)
# ---------------------------------------------------------------------------

@dataclass
class InMemoryRollingBuffer:
    """A simple in-memory ``RollingBuffer`` implementation.

    Useful for unit tests and as a server-side analysis hook (e.g. a
    an admin diagnostic endpoint that dumps the buffer contents).
    The browser-side implementation lives in JavaScript; this is
    the Python twin, kept structurally identical so verification can
    run the same shape checks.

    The buffer enforces two bounds:

    1. ``config.max_entries`` — older entries are evicted as new
       ones arrive (circular-buffer semantics).
    2. ``config.max_bytes_per_entry`` — each individual entry must
       be smaller than this ceiling.
    """

    session_id: str
    config: RollingBufferConfig = field(default_factory=RollingBufferConfig)
    _entries: list[RollingBufferEntry] = field(default_factory=list)

    def append(self, entry: RollingBufferEntry) -> None:
        """Add an entry, evicting the oldest if at capacity."""

        # Validate encoded bytes length explicitly (not just trust size_bytes).
        approx_decoded = _approx_decoded_size(entry.encoded)
        if approx_decoded > self.config.max_bytes_per_entry:
            raise BufferFlushError(
                f"entry payload exceeds max_bytes_per_entry "
                f"({approx_decoded} > {self.config.max_bytes_per_entry})."
            )

        self._entries.append(entry)
        # Evict oldest until within max_entries AND total size.
        while len(self._entries) > self.config.max_entries or self.total_bytes() > self.config.max_total_bytes:
            self._entries.pop(0)

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self) -> Iterator[RollingBufferEntry]:
        return iter(self._entries)

    def window_start(self) -> datetime | None:
        return self._entries[0].captured_at if self._entries else None

    def window_end(self) -> datetime | None:
        return self._entries[-1].captured_at if self._entries else None

    def total_bytes(self) -> int:
        return sum(e.size_bytes for e in self._entries)

    def to_serializable(self) -> dict:
        """Return a JSON-shaped dict for upload transport model."""
        return {
            "session_id": self.session_id,
            "config": {
                "capture_interval_ms": self.config.capture_interval_ms,
                "window_seconds": self.config.window_seconds,
            },
            "captured_window_start": (
                self.window_start().isoformat() if self.window_start() else None
            ),
            "captured_window_end": (
                self.window_end().isoformat() if self.window_end() else None
            ),
            "entries": [
                {
                    "captured_at": entry.captured_at.isoformat(),
                    "frame": entry.encoded,
                    "encoding": entry.encoding,
                    "resolution": list(entry.resolution),
                    "size_bytes": entry.size_bytes,
                }
                for entry in self._entries
            ],
        }


# ---------------------------------------------------------------------------
# Null implementation
# ---------------------------------------------------------------------------

@dataclass
class NullRollingBuffer:
    """A no-op ``RollingBuffer`` for tests / diagnostic paths.

    ``append`` is a no-op, ``__len__`` is zero, iterators are empty.
    The class satisfies the :class:`RollingBuffer` protocol shape
    without holding any state.
    """

    session_id: str = ""
    config: RollingBufferConfig = field(default_factory=RollingBufferConfig)

    def append(self, entry: RollingBufferEntry) -> None:
        """Drop the entry on the floor — no-op."""

        del entry  # Explicit no-op; do not store.

    def __len__(self) -> int:
        return 0

    def __iter__(self) -> Iterator[RollingBufferEntry]:
        return iter(())

    def window_start(self) -> datetime | None:
        return None

    def window_end(self) -> datetime | None:
        return None

    def total_bytes(self) -> int:
        return 0

    def to_serializable(self) -> dict:
        return {
            "session_id": self.session_id,
            "captured_window_start": None,
            "captured_window_end": None,
            "entries": [],
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _approx_decoded_size(encoded: str) -> int:
    """Return the approximate decoded byte count of a base64 string.

    Used by the buffer's per-entry size check without fully decoding
    the bytes.  Base64 expands data by roughly 4/3, so the encoded
    length minus trailing ``=`` padding is a slightly over-estimate
    of the true byte count.
    """

    if not encoded:
        return 0
    pad = encoded.count("=")
    return math.ceil((len(encoded) - pad) * 3 / 4)
