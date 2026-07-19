# Inference modules — design doc

One subsection per modality. Each covers: what runs, the exact input/output contract, thresholds, and — where the spec required a confidence interval — how that's actually computed rather than just asserted as a field name.

---

## 1. Face presence/count

**Model:** MediaPipe Face Detection (BlazeFace short-range), classic `mp.solutions` API. Runs client-side, every frame.

**Contract:** input is a single video frame; output is a list of detected faces, each with a bounding box and a detection confidence score.

**Second-person logic (ties to the zero-tolerance rule):** a face count ≥ 2 for 2–3 consecutive frames (the noise-filter window from the spec, not a leniency window) is what actually constitutes a confirmed second-person `TelemetryEvent`. A single frame reading 2 faces is not enough on its own — it could be a reflection or a compression artifact reading as face-like, which is exactly the noise category the confirmation window exists to filter, not tolerance for a person being briefly present.

**No-face case:** zero detected faces for a sustained period is a distinct signal from second-person — worth its own `flag_type` (`no_face`) rather than folding it into the same bucket, since "student stepped away" and "second person joined" call for different review framing even if both are concerning.

---

## 2. Identity match vs. enrollment

**Approach:** face embedding + cosine similarity against `EnrollmentReference.embedding_vector`. Library choice deferred per the spec (`face_recognition` vs. `DeepFace`) — this doc assumes whichever is chosen exposes "give me an embedding vector for this cropped face," since that's the actual interface this module needs regardless of which library backs it.

**Contract:** input is the cropped, aligned face region (output of the face-presence module, not the raw frame — see preprocessing doc). Output is a similarity score in [0, 1].

**Making the confidence interval real, not decorative.** A single similarity score from one frame is a point estimate, not a statistical interval. To actually satisfy the spec's confidence-interval requirement honestly: run identity match across the several frames captured within one sampling window (not just the single most recent one), and report the mean similarity plus the spread (e.g. mean ± standard deviation, or a simple min/max range across that window) as the interval. A `Flag` triggered off a single anomalous frame with no surrounding context is weaker evidence than one backed by a consistent multi-frame reading — this also naturally reduces false positives from one bad-lighting frame.

**Threshold:** match/no-match decision is a configurable similarity cutoff in `PolicyConfig`, not hardcoded — mirrors the same "don't bake a number into conditionals" principle used everywhere else in this spec.

---

## 3. Head pose / gaze

Already fully speced in the original document (§3.1) — restating the module contract here for completeness rather than re-deriving it:

**Input:** MediaPipe Face Mesh landmarks (478 points, iris-refined) from a heavy-check frame.

**Three derived signals:** (a) head pose (yaw/pitch/roll) via OpenCV `solvePnP` against the landmark set; (b) Eye Aspect Ratio from eye landmark points, to exclude blinks; (c) iris-position offset relative to eye-corner landmarks.

**Output:** a per-frame `off_screen: bool` classification, which Stage 2 (in the fusion engine, not this module) aggregates into `GazeAwayEvent`s. This module's responsibility ends at the per-frame classification — the event-aggregation and escalation-ladder logic belongs to the fusion engine, not here, to keep this module a stateless per-frame classifier rather than something that has to track rolling windows itself.

---

## 4. Object detection

Also fully speced (§3.2) — module contract:

**Model:** YOLOv8 (Ultralytics), pretrained on COCO, denylist scope (`cell phone`, `laptop`, `tv`, conditionally `book`).

**Input:** a heavy-check frame. **Output:** a list of detections, each with class label, confidence, and bounding box — filtered to only the denylisted classes before being emitted as `TelemetryEvent`s (anything else the model detects but isn't on the denylist is discarded here, not persisted, per the denylist-not-allowlist design).

**Book handling:** this module always emits a `TelemetryEvent` for a detected book regardless of exam type — it doesn't know the exam's `allowed_reference_materials` setting and shouldn't need to. Whether that event becomes a severity-escalating `Flag` is the fusion engine's job, checked against `ExamSession.allowed_reference_materials`.

---

## 5. Audio (voice activity / multiple speakers)

**Model:** `webrtcvad` for speech/silence detection — no external download, ships with the package.

**Input:** a preprocessed audio chunk at an accepted sample rate, split into accepted frame durations (see preprocessing doc).

**Output:** a per-frame speech/silence classification, plus a decibel level from the RMS calculation. **Aggregation:** consistent speech activity above a baseline noise floor, or a sustained elevated decibel level, is what becomes an `audio_anomaly` `TelemetryEvent` — not every individual VAD frame.

**Multi-speaker detection is explicitly not in v1**, per the spec — full diarization (`pyannote.audio`) needs Hugging Face-hosted models with license gating, flagged as a v2 candidate requiring separate verification rather than something built on an unverified assumption now.

---

## 6. Browser events

**No ML.** Native DOM listeners (`visibilitychange`, `blur`/`focus`, `fullscreenchange`, `copy`/`paste`/`contextmenu`) fire client-side and are forwarded as `browser_event` envelope messages (see ingestion doc). This module is really just the ingestion-layer message handler for this one message type — there's no separate "inference" step since there's nothing to classify beyond "this DOM event fired."

**Confidence field:** doesn't meaningfully apply here the way it does for the CV/audio modules — a `browser_event` `TelemetryEvent` can just carry `confidence: 1.0` as a constant, since there's no model uncertainty involved in "the tab lost focus," rather than forcing a fake probabilistic framing onto a deterministic signal.
