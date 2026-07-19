# AI Proctoring Engine — design doc index

Status: full v1 design complete across all layers. No code has been written yet — these are architecture/design documents. Each file below is self-contained but references the locked decisions in the original spec (`proctoring-engine-v1-spec.md`).

---

## How to read this set

| File | Layer | Covers |
|---|---|---|
| `proctoring-engine-v1-spec.md` | Requirements | Architecture decisions, all 6 modalities, gaze-away escalation, object-detection policy, evidence retention — the foundation everything below builds on |
| `01-data-models-design.md` | Data models | Every entity, field, type, relationship, and the two open sub-decisions (admin/reviewer identity, embedding storage) |
| `02-ingestion-layer-design.md` | Ingestion | LTI 1.3 launch flow, WebSocket message envelope, frame/audio capture format, reconnect handling |
| `03-preprocessing-layer-design.md` | Preprocessing | Frame decode/normalize per model, tiered sampling scheduler, the rolling buffer mechanism in procedural detail |
| `04-inference-modules-design.md` | Inference | Algorithmic detail for each of the 6 modalities — inputs, outputs, thresholds, confidence computation |
| `05-fusion-flagging-engine-design.md` | Fusion engine | How telemetry becomes a `Flag`, the three termination paths (zero-tolerance, gaze-frequency, accumulated-score), exemption suppression |
| `06-evidence-audit-store-design.md` | Evidence store | Storage key structure, flush mechanism, retention/deletion job, immutability enforcement |
| `07-api-orchestration-design.md` | API/orchestration | Route structure, session lifecycle state machine, authorization model |
| `08-test-strategy-design.md` | Test strategy | What gets tested per layer, boundary values, integration scenarios |

---

## Architectural flow (top to bottom)

```
Browser (client)
  — captures webcam/mic, runs lightweight checks (face presence, browser events)
  — maintains a short local rolling buffer (never transmitted unless a flag fires)
        │
        ▼
Ingestion & preprocessing
  — LTI 1.3 launch establishes the session; WebSocket carries telemetry up, kill-switch down
  — decodes frames, normalizes per model, schedules which check runs on which sample
        │
        ▼
Inference modules  (server-side heavy checks, one module per modality)
  — face presence/count · identity match · head pose/gaze · object detection · audio VAD · browser events
        │
        ▼
Fusion & flagging engine
  — zero-tolerance path (second person) → immediate CRITICAL
  — gaze-away frequency ladder → warning → CRITICAL
  — accumulated-score path (tab-blur, gaze-warnings) → CRITICAL if threshold crossed
  — accommodation exemptions checked before any flag involving an exempted object class is finalized
        │
        ├──────────────┬───────────────────────
        ▼                                      ▼
Evidence store                          Orchestration
  — persists flagged clips              — fires kill-switch over WebSocket
  — enforces retention_expires_at       — sends LTI callback to the LMS
                                         — writes the immutable TerminationRecord
```

---

## What's been decided vs. what's still open

**Fully locked (confirmed by you across this conversation):**
- Backend: Python 3.11+ / FastAPI, hybrid client/server inference
- Transport: WebSocket, tiered sampling
- Persistence: Postgres + S3-compatible object storage
- Scale target: moderate (tens–low hundreds of concurrent sessions)
- Termination: auto-terminate on zero-tolerance violation, configurable severity threshold
- Gaze-away: frequency-based escalation, sourced from your uploaded papers
- Object detection: denylist strategy, phone/laptop/2nd-screen in v1, earbuds/smartwatches explicitly deferred
- Accommodation exemptions: admin pre-approval, not self-declared
- Evidence retention: rolling buffer + context, not evidence-only or full-session recording

**Newly surfaced while writing these design docs — flagged clearly in the relevant file, not silently decided:**
- Who can approve an `AccommodationExemption` or `PolicyConfig` change — no `AdminUser`/reviewer entity has been defined yet (see `01-data-models-design.md`)
- Whether embeddings live in a `pgvector` column or an application-computed array (see `01-data-models-design.md`)
- The exact mechanism for the accumulated-score termination path — proposed in `05-fusion-flagging-engine-design.md`, not something you'd explicitly confirmed before now

Each of those is called out inline where it appears, not buried — worth a look before we move to code.
