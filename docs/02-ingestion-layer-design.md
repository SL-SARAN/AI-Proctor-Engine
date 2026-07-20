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

---

## 6. Implementation status (per `SYSTEM_STATE.md`)

The LTI 1.3 ingestion layer is being built in two turns, per the
"one atomic layer per turn" rule in `SKILLS_ALIGNMENT.md` §5.

**Turn N — foundation (this commit):** the boundary modules that
the launch routes and service depend on. 70 unit tests passing on
SQLite in `pytest tests --ignore=tests/integration`.

- `src/proctoring_engine/lti/config.py` — `LtiSettings` (frozen,
  slots, env-loaded). Validates `session_token_secret` is at least
  32 bytes. Includes `oidc_discovery_url` / `oidc_jwks_url` override
  fields, `tool_client_id`, `launch_url`, `nonce_store_ttl_seconds`,
  and the session-token shape parameters (`session_token_secret`,
  `session_token_ttl_seconds=14400`,
  `session_token_issuer="proctoring-engine"`,
  `session_token_audience="proctoring-client"`).
- `src/proctoring_engine/lti/claims.py` — Pydantic v2 models for
  the LTI 1.3 namespaced claims (`LtiIdToken` with aliased field
  names, plus `LtiContext`, `LtiResourceLink`, `LtiToolPlatform`,
  `LtiCustomClaims`). The parse boundary
  (`LtiIdToken.from_jwt_payload`) applies the LTI-version check
  (`1.3.0`) and the message-type check
  (`LtiResourceLinkRequest`) so a wrong version or unsupported
  message type surfaces as `LtiClaimsError` before the typed model
  is built. `require_policy_config_name(claims)` and
  `combined_context_id(claims)` are the two helpers the launch
  service uses.
- `src/proctoring_engine/lti/roles.py` — `AppRole` enum
  (`LEARNER`, `INSTRUCTOR`, `ADMIN`, `PROCTOR`) and the
  `map_roles(role_uris)` function. Maps per the LTI 1.3 role URI
  vocabularies (`Learner` → `LEARNER`, `Instructor` / `Faculty` /
  `TeachingAssistant` / `ContentDeveloper` → `INSTRUCTOR`,
  `Administrator` / `SysAdmin` / `SysSupport` → `ADMIN`, and the
  `Proctor` extension URI → `PROCTOR`). Highest privilege wins
  when multiple roles are present (admin > proctor > instructor >
  learner). `is_admin_route(role)` returns `True` for any
  non-learner role.
- `src/proctoring_engine/lti/state.py` — `LaunchStateStore`:
  in-memory, thread-safe, TTL-bounded (default 600 s) store of
  pending launches. The `consume` operation is atomic with respect
  to a concurrent `consume` for the same state — at most one caller
  wins, the rest see `LaunchStateMissing`. Uses a monotonic clock
  (`time.monotonic`) so system clock adjustments cannot extend or
  shorten the TTL window. Tests pass an explicit `clock=` for the
  expiration boundary. State and nonce values are 32-byte
  URL-safe tokens (`secrets.token_urlsafe(32)`).
- `src/proctoring_engine/lti/session_token.py` — HS256-signed
  session token. `issue_session_token(participant_id, exam_session_id,
  role, *, settings, now)` returns a JWT with the standard
  `iss` / `aud` / `exp` / `iat` claims plus `sub` (participant id),
  `sid` (exam session id), `role`, and `jti`. `decode_session_token`
  enforces signature, `iss`, `aud`, `exp`, and the required claim
  set; a missing or wrong-typed claim surfaces as
  `SessionTokenError`. TTL is 14 400 s (4 h).
- `src/proctoring_engine/lti/discovery.py` — `OidcDiscoveryCache`:
  process-local cache of OIDC discovery documents, keyed by issuer.
  `fetch(issuer, *, http_client, override_url)` returns the parsed
  `OidcDiscovery` dataclass (`authorization_endpoint`, `jwks_uri`,
  `token_endpoint`, `issuer`). 5 s HTTP timeout. The fetched
  document's `issuer` is validated against the requested issuer to
  defeat a misconfigured platform that points to the wrong
  discovery document.
- `src/proctoring_engine/lti/jwks.py` — `JwksCache`: TTL-bounded
  (default 600 s) cache of JWKS documents, keyed by URI. The
  `get_key(jwks_uri, kid, *, http_client)` call refreshes on a
  cache miss, on a stale entry, and on a kid-miss after the first
  refresh (the second refresh is the key-rotation handling — a
  kid that's still missing after that is a hard error). Keys
  without a `kid` are silently skipped (per RFC 7517 §4.5).
  `invalidate(jwks_uri)` is the manual flush for an admin endpoint
  that wants to force a key refresh.

**Turn N+1 — routes + service + integration tests (next):** the
FastAPI router (`GET /lti/login`, `POST /lti/launch`), the
`process_launch` service, the OIDC test double, and the
PostgreSQL integration suite. Documented in
`CONTEXT.md` §9.
