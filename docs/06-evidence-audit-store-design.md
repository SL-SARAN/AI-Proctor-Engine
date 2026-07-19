# Evidence & audit store — design doc

Covers how a flagged clip actually gets from the client's rolling buffer into durable storage, how it eventually gets deleted, and what guarantees the audit trail actually needs to hold up.

---

## 1. Storage key structure

Object storage keys follow a predictable hierarchy so a reviewer (or a deletion job) can locate evidence without a database round-trip if needed:

```
evidence/{exam_session_id}/{flag_id}/{artifact_type}.{ext}
```

e.g. `evidence/3f2a.../8b91.../video_clip.webm`. `EvidenceArtifact.storage_key` stores this full path; the DB row is still the source of truth for metadata (timestamps, checksum, retention date) — the key structure is a convenience for direct lookup and for scoping bucket lifecycle rules to a session prefix if that's ever useful.

---

## 2. Flush-to-storage flow

1. Fusion engine confirms a `Flag`.
2. Server sends a message back down the WebSocket telling the client to flush its rolling buffer (this can piggyback on the same message that carries `kill_switch` for CRITICAL flags, or be a lighter-weight notification for MEDIUM flags that don't terminate but still want evidence attached).
3. Client uploads the buffered clip.
4. Server writes the blob to object storage at the key above, computes a checksum, and only *then* inserts the `EvidenceArtifact` row — in that order, so a row is never created pointing at a blob that failed to actually land in storage.
5. `EvidenceArtifact.captured_window_start`/`captured_window_end` are set from the buffer's real timestamps (from the client), not server receipt time — preserves the actual "what was happening when" window for a reviewer.

---

## 3. Retention / deletion job

A scheduled worker (not part of the request-handling path) that:

1. Queries for `EvidenceArtifact` and `TelemetryEvent` rows where `retention_expires_at < now()`.
2. Deletes the object storage blob first, then the DB row — mirrors the flush order (never leave a DB row pointing at nothing, but here the goal is the reverse: never leave a blob with no corresponding row either, so deleting storage first and then confirming before removing the row is the safer failure mode if the job is interrupted mid-run).
3. Logs that a deletion occurred — session ID, artifact ID, timestamp of deletion — without retaining the deleted content itself. This is a real, if small, tension worth naming: the audit trail says "evidence existed and was deleted per policy," which is itself useful information, but that meta-record must not become a backdoor way of keeping the content around past its retention window.

This job is what actually makes `retention_expires_at` mean something — the field alone is inert without a process that acts on it.

---

## 4. Immutability enforcement

`Flag` and `TerminationRecord` rows should never be updated after creation. Two ways to enforce this, not mutually exclusive:

- **Application-level:** the ORM layer simply never exposes an update path for these models — inserts only, corrections happen via a new linked row (e.g. a `ProctorReview` overturning a `Flag`, not editing the `Flag` itself).
- **Database-level:** a trigger that rejects `UPDATE`/`DELETE` on these tables outright, as a backstop against a bug in the application layer accidentally mutating an audit record. Given this system's core selling point is defensible auditability, the DB-level backstop is worth the small extra setup — it protects against application bugs, not just deliberate misuse.

`ProctorReview` is the correct mechanism for "this flag was wrong" — it sits alongside the original `Flag`, not replacing it, so the full history (what was flagged, and what a human later decided about it) stays intact.

---

## 5. Consent gating at this layer

`ExamSession.consent_recorded_at` should be checked before *any* telemetry or evidence is persisted — this is really an ingestion-layer enforcement point (reject the WebSocket connection, or accept it but drop incoming telemetry, until consent is on record) more than something the evidence store itself checks per-write. Noting it here because it's the compliance field this whole layer's retention logic depends on, and it's worth being explicit that "the field exists" and "the field is actually enforced upstream" are two different things that both need to be true.
