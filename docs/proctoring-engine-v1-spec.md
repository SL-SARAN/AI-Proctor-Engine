# AI Proctoring Engine — v1 Technical Specification

Status: pre-implementation. This is the locked plan the Data Models layer (next turn) will be built against.

---

## 1. Locked architecture decisions

| Decision | Choice | Why |
|---|---|---|
| Backend language | Python 3.11+ / FastAPI | Async-native (needed for non-blocking sub-100ms checks); direct bindings to MediaPipe/OpenCV with no cross-language bridge |
| Inference location | Hybrid | Lightweight checks (face presence, browser events) run client-side in the browser for instant feedback; heavy checks (identity, gaze, objects, audio) run server-side |
| LMS integration | LTI 1.3 (OAuth2/JWT) | Industry-standard protocol supported by Canvas, Moodle, Blackboard, etc. — implementable as FastAPI routes, independent of the ML stack |
| Transport | WebSocket, tiered sampling | Client-side lightweight checks send only results (never raw frames); server-side heavy checks receive a downsampled frame every 2–3s (configurable), not full webcam rate. Same connection carries the kill-switch command back down. Chosen over WebRTC since v1 is fully automated checks, not live human-proctor video viewing — reopen WebRTC if that becomes a requirement, since it needs real STUN/TURN/SFU infrastructure this doesn't. |
| Persistence | Postgres (session/event/flag metadata) + S3-compatible object storage (evidence blobs) | Relational integrity + ACID transactions for the audit trail; object storage's lifecycle policies tie directly into `retention_expires_at` |
| Scale | **Thousands of concurrent sessions**, async worker pool, horizontally scalable via Kubernetes | Revised 2026-07-24 per explicit user confirmation — originally locked as "moderate concurrency (tens–low hundreds)"; heavy inference is still decoupled from request-handling via a task queue so a burst of frames doesn't block the event loop, but worker-pool and GPU sizing for the inference layer must now be planned against thousands of sessions, not low hundreds — this changes the batching/autoscaling design for the not-yet-built inference layer, and raises the priority of the WebSocket ingress-affinity limit already noted in `docs/DEPLOYMENT.md` |
| Compliance jurisdiction | **Not assumed** | I'm not a lawyer and won't hardcode retention/consent rules to a specific law. Data model will have generic `consent_recorded_at`, `retention_expires_at` fields you configure per your jurisdiction's requirements. |
| Termination policy | **Auto-terminate on zero-tolerance violation (v1)**, severity-threshold **configurable** in a policy table, not hardcoded | Per your instruction — changeable in later versions without a schema/code rewrite |

> **Amendment (2026-07-24):** Rows 1, 3, and 5 of §2 below were originally
> written against `mp.solutions` (legacy MediaPipe) and base `webrtcvad`.
> Both assumptions were invalidated during implementation and have been
> corrected here rather than left silently wrong in a "locked" document:
> - `mp.solutions` (Face Detection, Face Mesh) has been progressively
>   stripped from the `mediapipe` PyPI package since Google's 2023
>   deprecation announcement; confirmed via multiple live GitHub issues
>   that `mediapipe` 0.10.31+ raises `AttributeError: module 'mediapipe'
>   has no attribute 'solutions'`. This is a package-version issue, not a
>   Python-version issue — pinning to Python 3.11 does not restore it.
> - Base `webrtcvad` has no Windows wheel on PyPI and requires MSVC Build
>   Tools to compile from source on Windows — a long-standing, well-documented
>   limitation, not new.
> - Rows 1, 3, and 5 below now name the corrected libraries. The
>   underlying contracts (478 iris-refined landmarks, BlazeFace short-range
>   detection, EAR-based blink filtering, the VAD API shape) are unchanged
>   — only the library surface is corrected.

A note on library availability: model weight/bundle provenance differs by modality below. I'll flag per-modality which approach ships weights inside the pip package (verifiable in my dev sandbox) versus needs a runtime download from an external host (not on my sandbox's allowlist — verify reachability from your actual deployment environment, e.g. egress from your Kubernetes cluster to `storage.googleapis.com`, or bake the model file into the container image at build time instead of relying on a runtime fetch).

---

## 2. V1 modality implementation matrix

| # | Modality | Method | Runs | Model source | Output |
|---|---|---|---|---|---|
| 1 | Face presence/count | MediaPipe Tasks API, `mediapipe.tasks.python.vision.FaceDetector` (BlazeFace short-range — same underlying detector `mp.solutions` used) | Client, every frame | Model bundle (`.task` file) downloaded from `storage.googleapis.com` at first run, **not** bundled in the pip wheel — bake it into the client/container build rather than fetching at runtime | face count, confidence, bounding box(es) |
| 2 | Identity match vs enrollment | Face embedding + cosine similarity against enrollment photo | Server, every N seconds | **Needs a distinct library** — MediaPipe's own documentation is explicit that iris/face-mesh tracking "does not... provide any form of identity recognition." Candidates: `face_recognition` (dlib ResNet embeddings, pip-installable, weights ship with dlib) or `DeepFace` (wraps FaceNet/ArcFace, downloads weights from GitHub release assets at first run — verify in your environment). I'll confirm the exact choice at implementation time rather than assume. | similarity score (0–1), confidence interval |
| 3 | Head pose / "gaze" | MediaPipe Tasks API, `mediapipe.tasks.python.vision.FaceLandmarker` with `output_face_blendshapes=True` (confirmed: same 478 iris-refined landmarks `mp.solutions.face_mesh` produced) + OpenCV `solvePnP` for head pose; iris-position-relative-to-eye-corner as a coarse looking-away heuristic | Client (head pose) + server (fused with other signals) | Same `.task` model bundle as row 1's detector family — downloaded from `storage.googleapis.com` at first run, not bundled in the pip wheel | pitch/yaw/roll angles, on-screen/off-screen classification, confidence |
| 4 | Object detection — denylist (phone, laptop, 2nd screen; book conditional) | YOLOv8 (Ultralytics), pretrained on COCO — see 3.2 for scope and rationale | Server, every N seconds | Ultralytics YOLOv8 weights auto-download from GitHub Releases at first run — this host is on my sandbox's allowlist, unlike MediaPipe's newer Tasks-API bundles, so this is verifiable here | object class, confidence, bounding box |
| 5 | Audio (voice activity / multiple speakers) | `webrtcvad-wheels` (MIT-licensed fork of `webrtcvad`, identical `import webrtcvad` API) for speech/silence detection — base `webrtcvad` has no Windows wheel on PyPI and requires MSVC Build Tools to compile there; the fork ships prebuilt wheels for Windows/macOS/Linux across Python 3.6–3.13. Full multi-speaker diarization (e.g. `pyannote.audio`) needs Hugging Face-hosted models with license gating — **not proposed for v1** in favor of a simpler ambient-noise + voice-activity-above-baseline heuristic | Server, rolling audio chunks | VAD: bundled, prebuilt wheel, no compiler needed on any target OS. Diarization: flagged as a v2 candidate requiring separate verification | speech/silence flag, decibel level, (v2: speaker count) |
| 6 | Browser events (tab blur, fullscreen exit) | Native DOM events: `visibilitychange`, `blur`/`focus`, `fullscreenchange`, `copy`/`paste`/`contextmenu` | Client, event-driven (no polling) | No ML — plain JS listeners | event type, timestamp |

---

## 3. Zero-tolerance rule (as implemented)

- **No debounce window for "is this a second person legitimately passing by."** Your policy: nobody but the test-taker should ever be in frame. The moment person-detection confidence exceeds threshold for a second face/body, it's a violation — no leniency logic needed.
- **A short frame-confirmation window is still kept — for a different reason.** 2–3 consecutive confirming frames before firing, purely to filter *sensor noise* (motion blur, a glasses reflection, a compression artifact briefly reading as face-like) — not to give a second person time to leave. Every flag logs its confidence score and bounding box either way.
- **Termination flow (v1):** confirmed second-person detection → fusion engine emits a `CRITICAL` flag → session kill-switch fires: (a) WebSocket message instructs the exam client to lock/submit, (b) LTI callback notifies the LMS the session was terminated, (c) full evidence bundle (frames, timestamps, confidence, bounding boxes) is sealed to the audit log.
- **Configurability for v2:** the severity threshold and termination-vs-flag-for-review behavior live in a `PolicyConfig` table (not hardcoded conditionals), so switching to "flag only" later is a config change, not a redeploy.
- Tab-blur stays `MEDIUM` severity and accumulates a score rather than auto-terminating on its own.
- **Gaze-away is handled differently, per your instruction: it escalates to termination based on frequency, not on a single occurrence.** Detection pipeline and escalation logic below, sourced from the two papers in the project.

### 3.1 Gaze-away detection pipeline (sourced from uploaded literature)

**Stage 1 — per-frame classification.** MediaPipe Face Mesh (478 landmarks, iris-refined) feeds three signals: (a) head pose (yaw/pitch/roll) via OpenCV `solvePnP` against the landmark set — this is the approach the uploaded ICETETAS177 paper describes for detecting when a user is looking away from the screen; (b) Eye Aspect Ratio (EAR) computed from the eye landmark points, used to detect blinks/closure so a blink is never miscounted as looking away — also drawn from that paper's methodology; (c) iris-position offset relative to the eye-corner landmarks, used as a coarse "looking off to the side" signal. A frame is classified `off_screen` only when EAR indicates the eyes are open *and* either the head-pose angle or the iris offset exceeds a configurable threshold.

Carrying forward an earlier caveat: MediaPipe's own iris-model documentation states the model does not itself infer where someone is looking — so this is a geometric heuristic (head pose + iris offset), not calibrated point-of-gaze tracking. The systematic review among your uploaded sources (Malhotra & Chhabra) makes the same point from the literature side: dedicated eye-tracking hardware (e.g., Tobii) is more accurate than webcam-based gaze estimation, which is more prone to false detections from ordinary movement or lighting changes.

**Stage 2 — event aggregation (the frequency logic you asked for).** Raw per-frame `off_screen` classifications are noisy — both uploaded sources independently flag rapid/brief movements as a false-positive source if judged frame-by-frame. So consecutive `off_screen` frames are merged into a single `GazeAwayEvent` only once they persist past a minimum duration (default 800ms). Each confirmed event is logged with start/end timestamp, duration, and average deviation angle/offset — satisfying the confidence-interval requirement from the original spec.

**Escalation ladder** (configurable in `PolicyConfig`, not hardcoded):
- A rolling counter tracks `GazeAwayEvent` count within a configurable sliding window (default: 5 minutes).
- Below `gaze_warning_limit` (default: 3 events / 5 min) → logged only, no flag.
- At or above `gaze_warning_limit` → `MEDIUM` flag raised, visible to a human reviewer, contributes to the accumulated score.
- At or above `gaze_termination_limit` (default: 8 events / 5 min, or alternatively a cumulative session-lifetime count — your call) → `CRITICAL` flag → the same kill-switch flow as the second-person case (session lock, LMS callback, evidence bundle sealed).

One uploaded source gives a concrete numeric precedent worth noting: Ferdosi et al. (cited in the Malhotra & Chhabra review), tracking eyes/lips/head movement via MediaPipe landmarks with a k-NN classifier, used a 40% classification threshold to flag cheating behavior. It's a useful reference point, but that number came from their own labeled dataset — the defaults above should be treated as a starting point to calibrate against your own exam population's video, not a final answer.

### 3.2 Object detection policy (confirmed)

**Strategy: denylist, not allowlist.** The detector only searches for specific banned classes; anything it doesn't recognize is silently ignored. This matches both uploaded papers' approach (they target "mobile phones and electronic devices" specifically rather than trying to verify only permitted items are visible) and sidesteps false positives on desk clutter, pens, water bottles, etc. — none of which have a class in the model to begin with.

**v1 class scope**, using YOLOv8 pretrained on COCO as-is (no fine-tuning):
- `cell phone` — critical, always denylisted
- `laptop` — critical, always denylisted (a second computer in frame)
- `tv` — used as a proxy for an external second monitor. Worth flagging honestly: COCO's `tv` class was trained on actual televisions, not monitors specifically — validate its real-world hit rate on monitors during testing rather than assuming it transfers cleanly
- `book` — **conditional, not a blanket violation.** Whether a detected book is a violation depends on the exam's own permitted-materials setting, so this lives on `ExamSession` (e.g. `allowed_reference_materials: closed_book | open_book | specific_list`), not as a fixed rule in the object-detection policy itself. The detection event is always logged for audit either way; it only escalates severity when the exam is closed-book.

**Earbuds and smartwatches: explicitly out of scope for v1, by your decision.** Stock COCO has no class for either, and you chose Option A (skip, rather than fine-tune a custom model, try an open-vocabulary detector, or add a behavioral hand-to-ear proxy) after reviewing the tradeoffs. This is logged as a deliberate v1 gap, not an oversight — worth reopening once you have real exam-session recordings that could seed a properly diverse training set (the custom-fine-tune option), since building that dataset from your own population is safer than sourcing a generic one later.

**Accommodation exemptions: admin pre-approval, per your decision.** An ear-worn or wrist-worn device — moot for v1 detection today, but relevant the moment you revisit the earbud/smartwatch gap — should never be treated as a violation without checking for an approved exemption first. Concretely: exemptions are reviewed and recorded by an administrator *before* the exam (not self-declared by the test-taker mid-session), scoped to a specific participant and a specific object class, and referenced by the fusion engine to suppress or downgrade the relevant flag. See `AccommodationExemption` in the data model below.

---

## 4. Evidence retention strategy (confirmed)

**Rolling buffer + context.** Not every transmitted frame is stored — only frames/clips tied to an actual `Flag` become an `EvidenceArtifact`. But storing just the single triggering frame is too weak given the termination policy already in place: a disputed auto-termination needs a few seconds of context, not one still image, and the gaze-away escalation ladder is inherently about sequences of events, not isolated moments.

**Mechanism:** the client maintains a short continuous local buffer (capturing every 200–500ms, held only in browser memory, never transmitted during normal operation) — separate from the sparse 2–3s interval frames sent for routine heavy-check inference. Normal-case bandwidth is unaffected. The moment a flag fires, the client flushes that denser recent buffer to the server, which persists it as the `EvidenceArtifact` for that flag — giving a reviewer the lead-in to the violation, not just its instant.

---

## 5. Data model preview (entities only — full DDL is the next layer)

- `ExamSession` — links a participant, exam, LMS context, start/end time, status, `allowed_reference_materials` (closed_book / open_book / specific_list — governs whether a detected book escalates severity)
- `Participant` — identity, LMS user reference
- `AccommodationExemption` — admin-approved exemption tying a participant to a specific object class (e.g. hearing aid), reviewed before the exam, referenced by the fusion engine to suppress/downgrade that class's flags
- `EnrollmentReference` — enrollment photo + stored embedding vector
- `TelemetryEvent` — one raw per-modality signal reading (timestamped, confidence, raw value)
- `Flag` — a fused decision from the flagging engine (severity, contributing telemetry refs, confidence interval)
- `EvidenceArtifact` — stored frame/audio evidence tied to a flag; for flags backed by the client's rolling buffer, this is a short clip (flagged moment + lead-in context), not a single frame
- `PolicyConfig` — configurable severity thresholds and termination rules, including gaze-away parameters (`gaze_min_duration_ms`, `gaze_window_seconds`, `gaze_warning_limit`, `gaze_termination_limit`)
- `TerminationRecord` — immutable record of an auto-terminated session
- `ProctorReview` — human-in-the-loop override/annotation on any flag

---

## 6. Next step

Data Models layer: full DDL/ORM schema for the entities above, targeting Postgres, with the corresponding unit tests (boundary values on confidence scores, timestamp ordering, foreign key integrity).

All open decisions are now resolved — ready to write it whenever you say go.
