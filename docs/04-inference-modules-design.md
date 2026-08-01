# Inference modules — design doc

One subsection per modality. Each covers: what runs, the exact input/output contract, thresholds, and — where the spec required a confidence interval — how that's actually computed rather than just asserted as a field name.

---

## 1. Face presence/count

**Model:** MediaPipe Tasks API, `mediapipe.tasks.python.vision.FaceDetector` (BlazeFace short-range — the same underlying detector the original `mp.solutions.face_detection` used). Runs client-side, every frame.

> **Correction (2026-07-24):** originally speced against `mp.solutions`,
> which is no longer present in current `mediapipe` releases (confirmed:
> 0.10.31+ raises `AttributeError: module 'mediapipe' has no attribute
> 'solutions'`). The Tasks API `FaceDetector` is the direct replacement —
> same detector model, different call surface. One load-bearing
> difference: the model bundle (`.task` file) is downloaded from
> `storage.googleapis.com` at first run rather than bundled in the pip
> wheel. For the client-side (browser) case specifically, use the
> `@mediapipe/tasks-vision` NPM package's JS/WASM build, which has the
> equivalent contract — don't assume the Python package applies
> unmodified to the browser runtime.

**Contract:** input is a single video frame; output is a list of detected faces, each with a bounding box and a detection confidence score.

**Second-person logic (ties to the zero-tolerance rule):** a face count ≥ 2 for 2–3 consecutive frames (the noise-filter window from the spec, not a leniency window) is what actually constitutes a confirmed second-person `TelemetryEvent`. A single frame reading 2 faces is not enough on its own — it could be a reflection or a compression artifact reading as face-like, which is exactly the noise category the confirmation window exists to filter, not tolerance for a person being briefly present.

**No-face case:** zero detected faces for a sustained period is a distinct signal from second-person — worth its own `flag_type` (`no_face`) rather than folding it into the same bucket, since "student stepped away" and "second person joined" call for different review framing even if both are concerning.

---

## 2. Identity match vs. enrollment

**Approach:** face embedding + cosine similarity against `EnrollmentReference.embedding_vector`. **Library: `face_recognition` (dlib ResNet, 128-d embeddings) — resolved, not deferred** (turn N+4). A generic image embedder (MediaPipe's `ImageEmbedder`) was evaluated and rejected: it's a MobileNetV3 trained on ImageNet object classification, with no face-identity training at all — the wrong tool for a security-critical identity check, not merely a lower-accuracy one. `DeepFace` was the other real candidate; `face_recognition` was chosen for footprint (no TF/Keras) given the confirmed thousands-of-concurrent-sessions scale and the worker-pod memory ceiling in `docs/DEPLOYMENT.md`.

> **Packaging correction — not yet applied in code.** The current
> dependency (`face-recognition>=1.3,<2`) pulls in
> `face_recognition_models` 0.3.0, which depends on `pkg_resources` —
> fully removed in setuptools 82.0.0 (Feb 2026). The current test suite
> only has a Windows `pytest.importorskip` guard on this import, which
> does not touch the actual bug: `pkg_resources` removal affects every
> platform, including Linux production, not just Windows dev machines.
> Verified fix: swap in the maintained fork
> `face-recognition-models-ng` via a git URL pinned to a specific
> commit hash (not on PyPI — model files exceed the 100MB release
> limit). **Critical detail, verified by actually reproducing this**:
> `face_recognition`'s own package metadata hard-declares
> `Requires-Dist: face-recognition-models (>=0.3.0)` — pip resolves by
> distribution name, not import name, so simply adding the fork
> alongside the existing dependency is not sufficient; a normal
> `pip install face_recognition` will still try to fetch and reinstall
> the broken original package, silently overwriting the fork. The
> verified-safe sequence: install `face_recognition` with `--no-deps`,
> then its real dependencies (`click`, `numpy`, `Pillow`) explicitly,
> then `dlib` via `dlib-bin` (prebuilt wheel — also solves the Windows
> MSVC problem as a side effect, meaning the Windows test skip should
> be **removed**, not kept), then the pinned fork. `pip check` will
> permanently show two phantom "not installed" complaints for `dlib`
> and `face-recognition-models` — expected, not a bug to "fix" by
> installing the real broken packages.
>
> **Runtime-failure handling — designed, not yet built.** Lazy import
> inside the backend constructor; `ImportError`/`SystemExit` propagate
> as a clear runtime error, never a silent stub. Default: **the exam
> session blocks entirely** rather than proceeding with identity
> unverified — this is the one check nothing else in the pipeline
> compensates for. Escape hatch: a scoped, time-bounded admin override
> requiring two-person approval (a professor/admin requests with a
> required reason; any `AdminUser` holding the new `HEAD` role for that
> department must approve; resolved ahead of the exam, not live;
> fails closed with no escalation if no Head responds). See the new
> `IdentityVerificationOverrideRequest` entity in
> `01-data-models-design.md`. The override lifts the block only —
> `ExamSession.identity_verification_status` still records
> `unavailable`, a mandatory-review `Flag` is still raised, and grade
> release still holds pending that review for sessions run under an
> override.

**Contract:** input is the cropped, aligned face region (output of the face-presence module, not the raw frame — see preprocessing doc). Output is a similarity score in [0, 1].

**Making the confidence interval real, not decorative.** A single similarity score from one frame is a point estimate, not a statistical interval. To actually satisfy the spec's confidence-interval requirement honestly: run identity match across the several frames captured within one sampling window (not just the single most recent one), and report the mean similarity plus the spread (e.g. mean ± standard deviation, or a simple min/max range across that window) as the interval. A `Flag` triggered off a single anomalous frame with no surrounding context is weaker evidence than one backed by a consistent multi-frame reading — this also naturally reduces false positives from one bad-lighting frame.

**Threshold:** match/no-match decision is a configurable similarity cutoff in `PolicyConfig`, not hardcoded — mirrors the same "don't bake a number into conditionals" principle used everywhere else in this spec.

---

## 3. Head pose / gaze

Already fully speced in the original document (§3.1) — restating the module contract here for completeness rather than re-deriving it:

**Input:** MediaPipe Tasks API `FaceLandmarker` output (478 points, iris-refined, via `output_face_blendshapes=True`) from a heavy-check frame — confirmed the same landmark count and iris refinement the original `mp.solutions.face_mesh` spec assumed; see the correction note in §1 above for why this is `FaceLandmarker` rather than `face_mesh`.

**Three derived signals:** (a) head pose (yaw/pitch/roll) via OpenCV `solvePnP` against the landmark set; (b) Eye Aspect Ratio from eye landmark points, to exclude blinks; (c) iris-position offset relative to eye-corner landmarks.

**Output:** a per-frame `off_screen: bool` classification, which Stage 2 (in the fusion engine, not this module) aggregates into `GazeAwayEvent`s. This module's responsibility ends at the per-frame classification — the event-aggregation and escalation-ladder logic belongs to the fusion engine, not here, to keep this module a stateless per-frame classifier rather than something that has to track rolling windows itself.

---

## 4. Object detection

Also fully speced (§3.2) — module contract:

**Model:** YOLOv8 (Ultralytics), pretrained on COCO, denylist scope (`cell phone`, `laptop`, `tv`, conditionally `book`).

> **Weights correction (resolved turn N+4, code matches):** force-bundle
> the `.pt` file via `YOLO_WEIGHTS_PATH`, not the default
> `YOLO('yolov8n.pt')` auto-download at init — matching the MediaPipe
> `.task` bundle pattern. Ultralytics' download mechanism resolves a
> pinned release tag by default (reproducible, unlike the earlier
> MediaPipe "latest" concern), but it first calls `api.github.com` to
> resolve the asset list, and that endpoint has a strict 60
> requests/hour/IP unauthenticated limit — a real risk when several
> pods cold-start behind the same cluster egress IP during an HPA
> scale-up burst. This is already reflected in the code
> (`YOLO_WEIGHTS_PATH` env-var guard, per `SYSTEM_STATE.md`).

**Input:** a heavy-check frame. **Output:** a list of detections, each with class label, confidence, and bounding box — filtered to only the denylisted classes before being emitted as `TelemetryEvent`s (anything else the model detects but isn't on the denylist is discarded here, not persisted, per the denylist-not-allowlist design).

**Book handling:** this module always emits a `TelemetryEvent` for a detected book regardless of exam type — it doesn't know the exam's `allowed_reference_materials` setting and shouldn't need to. Whether that event becomes a severity-escalating `Flag` is the fusion engine's job, checked against `ExamSession.allowed_reference_materials`.

---

## 5. Audio (voice activity / multiple speakers)

**Model:** `webrtcvad-wheels` for speech/silence detection — a MIT-licensed fork of `webrtcvad` with an identical `import webrtcvad` API, ships prebuilt wheels for Windows/macOS/Linux across Python 3.6–3.13.

> **Correction (2026-07-24):** base `webrtcvad` has no Windows wheel on
> PyPI and fails to build there without MSVC Build Tools (a long-standing,
> well-documented limitation — confirmed via multiple GitHub issues going
> back to 2019, not something new or sandbox-specific). `webrtcvad-wheels`
> is a drop-in replacement: same module name (`webrtcvad`), same class
> (`webrtcvad.Vad`), same `is_speech()` call shape — no code changes
> needed beyond the `pyproject.toml` dependency line.

**Input:** a preprocessed audio chunk at an accepted sample rate, split into accepted frame durations (see preprocessing doc).

**Output:** a per-frame speech/silence classification, plus a decibel level from the RMS calculation. **Aggregation:** consistent speech activity above a baseline noise floor, or a sustained elevated decibel level, is what becomes an `audio_anomaly` `TelemetryEvent` — not every individual VAD frame.

**Multi-speaker detection is explicitly not in v1**, per the spec — full diarization (`pyannote.audio`) needs Hugging Face-hosted models with license gating, flagged as a v2 candidate requiring separate verification rather than something built on an unverified assumption now.

---

## 6. Browser events

**No ML.** Native DOM listeners (`visibilitychange`, `blur`/`focus`, `fullscreenchange`, `copy`/`paste`/`contextmenu`) fire client-side and are forwarded as `browser_event` envelope messages (see ingestion doc). This module is really just the ingestion-layer message handler for this one message type — there's no separate "inference" step since there's nothing to classify beyond "this DOM event fired."

**Confidence field:** doesn't meaningfully apply here the way it does for the CV/audio modules — a `browser_event` `TelemetryEvent` can just carry `confidence: 1.0` as a constant, since there's no model uncertainty involved in "the tab lost focus," rather than forcing a fake probabilistic framing onto a deterministic signal.
