# Ingestion layer — design doc

Covers how a session gets established through the LMS, and the exact shape of data flowing over the WebSocket in both directions.

---

## 1. LTI 1.3 launch flow (session establishment)

This follows the standard LTI Advantage / OIDC third-party-initiated login flow — not something specific to this project, but worth laying out precisely since the session's identity and authorization all derive from it:

1. **Login initiation.** The LMS (platform) redirects the student's browser to this tool's login-initiation endpoint, passing `iss` (platform issuer), `login_hint`, `target_link_uri`, and `lti_message_hint`.
2. **Auth request.** The tool responds by redirecting back to the platform's own OIDC authorization endpoint, with `client_id`, `redirect_uri`, `state`, and `nonce` — typically via `response_mode=form_post`.
3. **Platform authenticates and redirects back.** The platform redirects to the tool's registered launch URL with a signed `id_token` (JWT) containing the LTI claims: message type, `deployment_id`, `resource_link` (the exam), `context` (course), `roles`, and the `state` value from step 2.
4. **Validation.** The tool validates the JWT signature against the platform's public JWKS endpoint, checks `nonce` and `state` match what was issued, and confirms `roles` before creating the session.
5. **Session creation.** On success: create or look up the `Participant` row (keyed on `lms_user_id` + `lms_context_id`), create the `ExamSession` row (`status: pending`), and issue a short-lived session token the browser uses to open the WebSocket.

**Role-based branching:** an instructor/admin-role launch should route to the admin surfaces (policy config, accommodation exemptions, review queue) instead of the exam-taking client — same launch mechanism, different destination based on the `roles` claim.

---

## 2. WebSocket connection

**Endpoint pattern:** one connection per exam session, established after the LTI launch using the session token issued in step 5 above for authentication (passed as a query param or subprotocol header at connect time — the WebSocket handshake itself is a plain HTTP request, so the token travels the same way a normal auth header would).

**Heartbeat:** a ping/pong every ~15s. No pong within a grace window (proposed: 30s) doesn't mean instant termination — it means a `connection_lost` `MEDIUM` flag, since a dropped connection could be a network blip, not misconduct. This is a real design fork worth flagging: silence isn't evidence of a violation, and treating it as one would punish students with bad Wi-Fi. Escalation from repeated drops to something more serious should go through the same accumulated-score path as other `MEDIUM` signals, not a special case.

**Reconnect:** the client should be able to reconnect with the same session token and resume the same `ExamSession` row rather than creating a new one, as long as the session hasn't already reached a terminal `status`.

---

## 3. Message envelope (client → server)

Every message is a JSON object with a consistent envelope:

```json
{
  "type": "telemetry_light | telemetry_heavy_frame | audio_chunk | browser_event",
  "session_id": "uuid",
  "captured_at": "ISO-8601 timestamp, set client-side",
  "payload": { }
}
```

**`telemetry_light`** — results only, never raw pixels, from the client-side checks (face presence/count, and any client-computed head-pose values before server fusion):
```json
{ "modality": "face_presence", "face_count": 1, "confidence": 0.97, "bbox": [x, y, w, h] }
```

**`telemetry_heavy_frame`** — an actual frame, sent at the sparse interval (default 2–3s, configurable) for server-side inference:
```json
{ "frame": "<base64 or binary JPEG>", "resolution": [w, h], "encoding": "jpeg" }
```
Frame resolution and quality are tunable, not fixed by anything inherent to the models — a reasonable starting point is a modest resolution (e.g. in the 480p range) and JPEG quality tuned to keep payload size small, since these get sent every few seconds for the whole exam duration. Treat the exact numbers as something to tune against real bandwidth/accuracy testing, not settle here.

**`audio_chunk`** — raw or lightly encoded audio for VAD:
```json
{ "audio": "<base64 PCM or Opus>", "sample_rate_hz": 16000, "duration_ms": 2000 }
```
Sample rate matters concretely here: `webrtcvad` (the library named in the spec for voice-activity detection) only accepts 8000, 16000, 32000, or 48000 Hz input, and only processes frames of exactly 10, 20, or 30 ms — the client should resample/chunk to one of those combinations before sending, rather than pushing that conversion onto the server.

**`browser_event`** — DOM-level signals, event-driven, no polling:
```json
{ "event_type": "visibilitychange | blur | focus | fullscreenchange | copy | paste | contextmenu", "detail": {} }
```

---

## 4. Message envelope (server → client)

```json
{
  "type": "ack | kill_switch | policy_update",
  "payload": { }
}
```

**`kill_switch`** is the important one — the single message that instructs the exam client to lock the interface and force-submit:
```json
{ "reason": "second_person_detected | gaze_frequency_exceeded | accumulated_score_exceeded", "flag_id": "uuid" }
```

The client's obligation on receipt: immediately disable further input, submit whatever answer state exists, display a neutral "this session has ended" message (not an accusatory one — the human review process, not the client UI, is where a disputed termination gets adjudicated), and send a final acknowledgment back so `TerminationRecord.kill_switch_ack_at` can be set.

---

## 5. What this layer deliberately does not do

It doesn't run any inference itself — it validates, authenticates, decodes envelopes, and hands payloads to the preprocessing layer. Keeping ingestion "dumb" (parse and forward) rather than embedding modality-specific logic here is what keeps the tiered-sampling rates and the inference modules independently tunable without touching the connection-handling code.
