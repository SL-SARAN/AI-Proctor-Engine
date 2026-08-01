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
- Scale target: **thousands of concurrent sessions** (revised
  2026-07-24 from the original "moderate, tens–low hundreds" figure —
  see `SYSTEM_STATE.md` §4)
- Termination: auto-terminate on zero-tolerance violation, configurable severity threshold
- Gaze-away: frequency-based escalation, sourced from your uploaded papers
- Object detection: denylist strategy, phone/laptop/2nd-screen in v1, earbuds/smartwatches explicitly deferred
- Accommodation exemptions: admin pre-approval, not self-declared
- Evidence retention: rolling buffer + context, not evidence-only or full-session recording
- **WebSocket gateway: Cloudflare Durable Objects** (resolved
  2026-07-25) — not a hand-built stateful gateway. Reuses the
  Cloudflare dependency already in the topology rather than adding a
  new vendor or owning bespoke health-check/failover code.
- **Accumulated-score termination path: wanted, single accumulator**
  across all MEDIUM signals (resolved 2026-07-25) — see the full
  design in `05-fusion-flagging-engine-design.md`, including the
  admin-configurable terminate-vs-flag-for-review action and the
  live-proctor fast-track-undo mechanism, both of which supersede
  turn N+5's separate "termination is final" call (see that doc for
  why).
- **Identity-match library: `face_recognition`** (dlib ResNet), not a
  generic embedder — resolved 2026-07-25, see
  `04-inference-modules-design.md` §2 for the full reasoning and the
  packaging fix it depends on.

**Resolved since this doc was first written (previously listed as open):**
- **Admin/reviewer identity** — `AdminUser` table exists as part of
  the initial schema (`01-data-models-design.md`); `approved_by`,
  `created_by`, `reviewer_reference` still exist as string fields for
  backward compatibility but the structured FK path is built.
- **Embedding storage** — settled for v1 as a JSONB float array;
  `pgvector` revisitable only if a "search across many embeddings"
  use case appears.

**Still genuinely open:**
- The session-token delivery mechanism for LTI launches (query
  parameter today; whether to move to a URL fragment) — see
  `02-ingestion-layer-design.md` §6 note.
- `PolicyConfig.name` uniqueness vs. its own versioning promise — a
  real bug surfaced by Claude Code itself at turn N+7, fix proposed
  but not yet applied (see `SYSTEM_STATE.md` §2, item 8).
- The identity-match backend's runtime-failure handling (fail-closed
  default, break-glass two-person-approved override) — designed, not
  yet implemented; today's code only has a Windows-skip test, which
  does not touch the underlying packaging failure.
