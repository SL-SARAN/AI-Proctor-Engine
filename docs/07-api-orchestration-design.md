# API / orchestration layer — design doc

The FastAPI route surface, the session lifecycle state machine, and how authorization is decided from the LTI role claim rather than a separately-built permission system.

---

## 1. Route structure

| Route | Method | Who | Purpose |
|---|---|---|---|
| `/lti/login` | GET/POST | Platform-initiated | OIDC login initiation (step 1 of the launch flow) |
| `/lti/launch` | POST | Platform-initiated | Receives and validates the `id_token`, creates `Participant`/`ExamSession`, issues the session token |
| `/ws/session/{session_id}` | WebSocket | Student client | Telemetry up, kill-switch/policy messages down — see ingestion doc |
| `/sessions/{id}/status` | GET | Student client | REST fallback for session status if the WebSocket needs a lightweight polling backstop |
| `/sessions/{id}/terminate` | POST | Internal (fusion engine) or admin | Explicit manual termination path, distinct from the automatic kill-switch — lets a human proctor end a session directly without waiting for an automated flag |
| `/admin/policy-config` | GET/POST | Admin/instructor role | CRUD for `PolicyConfig` — POST creates a new *version*, never mutates an existing one (see data-models doc) |
| `/admin/accommodation-exemptions` | GET/POST | Admin/instructor role | The pre-approval workflow — create/list exemptions before an exam starts |
| `/admin/flags/{session_id}` | GET | Admin/instructor role | Review queue — lists `Flag`s for a session, with linked `EvidenceArtifact` |
| `/admin/flags/{flag_id}/review` | POST | Admin/instructor role | Creates a `ProctorReview` |

---

## 2. Session lifecycle state machine

```
pending → active → completed
                 → terminated   (via automatic kill-switch or manual /terminate)
                 → under_review (set when a ProctorReview is pending on any of its flags)
```

**Transitions worth being explicit about:**
- `pending → active` happens on the first successful WebSocket connection, not at LTI launch — a student can launch the tool and not yet have started the actual proctored session.
- `active → terminated` is the only transition the fusion engine can trigger automatically; every other transition is either student-driven (`completed`, on normal submission) or admin-driven (manual `/terminate`, or moving into `under_review`).
- A `terminated` session can still move to `under_review` afterward — termination and review aren't mutually exclusive states, since every auto-termination should probably get reviewed, not just disputed ones.

---

## 3. Authorization model

No separate permission system — authorization derives directly from the `roles` claim already present in the LTI `id_token` from the launch flow. A launch carrying an instructor/admin role routes to the `/admin/*` surfaces; a learner-role launch routes to the exam-taking WebSocket flow. This keeps authorization tied to the LMS's own role model rather than duplicating a second source of truth for who's allowed to do what — if the institution changes someone's role in the LMS, that's reflected here automatically on their next launch, not through a separate admin panel that could drift out of sync.

**The one internal exception:** the fusion engine calling `/sessions/{id}/terminate` isn't an LTI-authenticated actor — this needs a distinct internal service-to-service credential (not tied to any student or instructor identity), separate from the LTI-derived authorization used everywhere else.

---

## 4. What this layer deliberately doesn't own

It doesn't make flagging or termination *decisions* — those live in the fusion engine. This layer's job is routing, authentication/authorization, and state transitions triggered by decisions made elsewhere. Keeping decision logic and orchestration logic in separate layers is what makes the fusion engine's rules (zero-tolerance, gaze-frequency, accumulated-score) testable in isolation, without needing a live WebSocket connection or LTI context to exercise them.
