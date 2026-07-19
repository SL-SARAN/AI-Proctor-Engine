# Preprocessing layer — design doc

This sits between ingestion (raw envelopes arriving) and the inference modules (which expect clean, model-ready input). Its job: decode, normalize per model, decide what runs when, and manage the client-side rolling buffer.

---

## 1. Frame decode and per-model normalization

A `telemetry_heavy_frame` arrives as encoded JPEG bytes. Decoding is one step (JPEG → array), but normalization differs by which inference module consumes it next:

- **MediaPipe models** (Face Mesh for gaze, Face Detection for presence) expect RGB input — a decode step that assumes BGR (OpenCV's default) needs an explicit channel-order conversion before the frame reaches these models, or landmarks will be computed against the wrong channel order.
- **YOLOv8 (object detection)** has its own internal preprocessing (resize/letterbox to the model's expected input size, pixel normalization) handled by the Ultralytics library itself when you pass it a raw array — this layer's job is just to hand it a correctly-decoded array, not to duplicate that normalization.
- **Face embedding model** (identity match) — most embedding models expect a tightly cropped, aligned face region, not the full frame. This means the pipeline order matters: face detection must run first to get a bounding box, then the embedding model runs on the cropped region, not on the raw frame independently. Worth designing the module interfaces so identity-match explicitly depends on face-presence output rather than running blind on its own crop logic.

---

## 2. Tiered scheduling

Different modalities need different sampling cadences, and the preprocessing layer is where that gets decided per incoming frame/chunk, not left implicit:

| Modality | Cadence | Why |
|---|---|---|
| Face presence/count | Client-side, every frame | Cheapest check, needs instant feedback, never touches the server |
| Head pose / gaze | Server, on every received heavy frame (2–3s interval) | Needs to run often enough that the 800ms minimum-duration aggregation (spec §3.1) has enough samples to actually confirm an event, not miss it between samples |
| Identity match | Server, sparser than gaze — e.g. every N heavy frames, not every one | Heaviest per-call cost of the modules; doesn't need to run as often since identity doesn't change frame-to-frame |
| Object detection | Server, on every received heavy frame or every other one | Needs to catch a phone that appears briefly — sparser than gaze risks missing a short appearance entirely |
| Audio VAD | Server, on every received audio chunk (independent cadence from video, since it's a separate stream) | |
| Browser events | Client, event-driven | No sampling decision needed — these fire only when the DOM event fires |

**The actual scheduling decision this layer owns:** given the fixed 2–3s heavy-frame interval from the transport design, how many of the fixed inference cadences share the same underlying frames versus needing an independent higher rate. Gaze and object detection can both run on the same arriving frame; identity match doesn't need to run on every one of them. This is a real tuning question, not a fixed constant — flagging it explicitly as something to calibrate against real latency measurements once code exists, rather than asserting exact numbers now.

---

## 3. The client-side rolling buffer, in procedural detail

This is the mechanism behind the "rolling buffer + context" evidence retention decision from the spec. Restated as an actual procedure:

1. The browser client maintains a circular buffer in memory — captures a frame every 200–500ms (denser than the 2–3s heavy-check interval), holds roughly the last 10–15 seconds, discards older frames as new ones arrive.
2. This buffer is never transmitted during normal operation — it exists purely so that *if* something fires, there's already-captured context sitting locally, rather than needing to somehow reconstruct the past after the fact.
3. When the fusion engine confirms a flag (see fusion-engine doc) and pushes a `kill_switch` or flag-notification message back down, the client flushes its current buffer contents up to the server as a single upload.
4. The server receives that flush, and the evidence-store layer persists it as an `EvidenceArtifact` (video_clip type) tied to the triggering `Flag`, with `captured_window_start`/`captured_window_end` set from the buffer's actual timestamps — not just "now."

**Why this lives client-side and not server-side:** the server only receives frames at the sparse 2–3s interval during normal operation — a server-side buffer built from those would contain a handful of far-apart stills, not a usable clip. Building the denser buffer client-side means normal-case bandwidth is completely unaffected (nothing extra is sent unless something actually fires), and the context clip is dense enough to be useful when it is.

---

## 4. Audio preprocessing

Incoming audio chunks are decoded to raw PCM if they arrived encoded, then resampled if necessary to one of the four rates `webrtcvad` accepts (8000/16000/32000/48000 Hz), then split into the fixed frame durations that library requires (10/20/30ms) before being handed to the audio VAD inference module. Decibel-level computation (for the ambient-noise heuristic in the spec) is a straightforward RMS calculation over the same chunk, done here rather than duplicated inside the inference module.
