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

> **Flagged, not yet resolved:** the LTI launch redirect
> (`redirect_url = f"{settings.exam_client_url}?session_token={token}"`,
> per turn N+1's `process_launch` implementation) puts the session
> token in a plain URL query parameter. This is a real, known
> anti-pattern — query params get logged by reverse proxies/CDNs
> along the path, leak via `Referer` headers on any outbound link,
> and persist in browser history. The standard fix is a URL
> **fragment** (`#session_token=...`) instead — fragments never get
> sent to the server or logged by intermediate infrastructure, and
> resolve entirely client-side, so the browser client reads it the
> same way. This already has 134 unit + 9 integration tests built
> around the query-param behavior (turn N+1) — worth a deliberate
> decision on whether to fix now (touching tested code) or accept as
> known debt, not something to silently carry forward into the
> browser-client layer that's about to consume this same redirect
> URL.

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

**Turn N+1 — routes + service + integration tests (this commit):**
closes the ingestion layer. The FastAPI router
(`GET /lti/login`, `POST /lti/launch`), the `process_launch`
service, the OIDC test double, and the PostgreSQL integration
suite. 134 unit tests + 9 PostgreSQL integration tests pass.

- `src/proctoring_engine/lti/service.py` — `process_launch(db,
  claims, role, *, settings, now) -> LaunchResult`. Pure
  function over a SQLAlchemy `Session` and a validated
  `LtiIdToken` + `AppRole` + `LtiSettings`. Steps:
  1. **Upsert `Participant`** on `(lti_issuer, lms_user_reference)`.
     `display_name` is set from the launch's `name` claim when
     present; if the row already exists and the new name is
     non-null, it's overwritten (the launch is the source of
     truth for the LMS-side display name). `consent_recorded_at`
     and `consent_notice_version` are **not** set on the
     participant here — consent is per-session, not
     per-participant, so the column stays null on the participant
     and is set on the `ExamSession` row at launch time.
  2. **Resolve `PolicyConfig`** by `name ==
     claims.custom.policy_config_name` AND `is_active == true`.
     If the name is missing or no active policy matches, raise
     `LtiLaunchError("policy_not_found")`. The route handler
     maps this to a 400 with a short error code.
  3. **Create `ExamSession`** with `status=PENDING`,
     `policy_config_id` bound to the resolved policy,
     `lti_issuer = claims.issuer`,
     `lti_context_id = f"{claims.context.id}:{claims.resource_link.id}"`
     (the same `combined_context_id` helper from
     `lti/claims.py`),
     `exam_reference = claims.resource_link.id`,
     `attempt_reference = uuid4()` (collision-free across
     platforms; the existing unique constraint enforces it),
     `consent_recorded_at = now(UTC)`,
     `started_at = consent_recorded_at` (the documented
     "consent is start" choice — the
     `ck_exam_session_timestamp_order` and
     `ck_exam_session_retirement_after_start` checks are
     self-consistent at creation time. The WS layer enforces
     the PENDING → ACTIVE transition separately).
  4. **Upsert `AdminUser`** when `is_admin_route(role)` is True.
     The natural key is `(lti_issuer, lms_user_reference)`. The
     `role` field stores the *highest* applicable tier from
     the launch (`AppRole.ADMIN` > `AppRole.PROCTOR` >
     `AppRole.INSTRUCTOR`), so the same instructor cannot be
     demoted by a later lower-privilege launch.
  5. **Issue the session token** via
     `lti.session_token.issue_session_token(participant_id,
     exam_session_id, role, settings=settings, now=now)`.
  6. **Return `LaunchResult(participant, exam_session,
     session_token, role, redirect_url)`** where
     `redirect_url` is:
     - For `AppRole.LEARNER`:
       `f"{settings.exam_client_url}?session_token={token}"`
     - For `AppRole.INSTRUCTOR` / `ADMIN` / `PROCTOR`:
       `f"{settings.admin_surface_url}?session_token={token}"`.
  7. **The service does not commit.** The route receives a
     `Session`, calls `process_launch`, then commits after
     `process_launch` returns so a single launch is a single
     atomic write. A failure between "participant upserted"
     and "exam session created" rolls back the whole launch —
     no orphan `Participant` rows from a partial launch.

- `src/proctoring_engine/lti/routes.py` — FastAPI `APIRouter`
  factory `_build_lti_router(deps)`. Returns a router with:
  - `GET /lti/login` — login initiation. Required query
    parameters: `iss`, `login_hint`, `target_link_uri`,
    `lti_message_hint`. Optional: `client_id` (when the
    platform uses an explicit client id). Generates a
    32-byte URL-safe `state` and a 32-byte URL-safe `nonce`,
    registers them in `LaunchStateStore` (with
    `redirect_uri = settings.launch_url` and
    `lti_issuer = iss`), fetches OIDC discovery for `iss` (or
    uses `settings.oidc_discovery_url` if set as an override
    for non-conformant platforms), and 302-redirects to the
    platform's authorization endpoint with
    `scope=openid`, `response_type=id_token`,
    `response_mode=form_post`, `prompt=none`,
    `client_id=settings.tool_client_id` (or the explicit
    `client_id` param when provided),
    `redirect_uri=settings.launch_url`, `login_hint`,
    `state`, `nonce`. Failure modes: missing required param →
    422; target_link_uri mismatch → 400 `claims_invalid`;
    discovery error → 502.
  - `POST /lti/launch` — launch callback. Body is
    `application/x-www-form-urlencoded` with a single
    `id_token` field. Validates the JWT against the
    platform's JWKS endpoint, parses the payload via
    `LtiIdToken.from_jwt_payload` (after stripping the
    OIDC `state` claim, which is not an LTI claim and
    `extra=forbid` rejects it), calls
    `state_store.peek(state)` to validate the `iss` claim
    against the state-registered issuer (a cross-issuer
    state-reuse attempt is rejected here, before the
    discovery fetch), then calls `state_store.consume(state,
    claims.nonce)` (one-shot, fail-closed), then calls
    `service.process_launch`. On success: 302 to the
    resolved `redirect_url`. On failure: 400 (or 502 for
    `discovery_error`) with a short error code from a
    closed enumeration (`policy_not_found`,
    `signature_invalid`, `state_expired`, `state_unknown`,
    `claims_invalid`, `nonce_mismatch`, `audience_invalid`,
    `issuer_invalid`) — the JWT contents are never echoed
    in the response.

  The factory takes the per-test dependencies via the
  `_RouterDeps` dataclass (`settings`, `state_store`,
  `jwks_cache`, `discovery_cache`, `http_client_factory`,
  `get_db`) so the route can use a process-shared
  `httpx.AsyncClient` in production and a `pytest-httpx`
  mock in tests.

- `tests/integration/oidc_test_double.py` — the OIDC test
  double. Generates an RSA keypair, builds the discovery
  document + JWKS payloads, and signs launch JWTs against
  the generated key. Exports `LEARNER_URI`, `INSTRUCTOR_URI`,
  `ADMIN_URI`, `PROCTOR_URI` role constants, plus
  `make_test_oidc_setup`, `register_oidc_responses`, and
  `build_signed_launch_claims`. Used by both the unit
  tests (`tests/test_lti_routes.py`,
  `tests/test_lti_service.py`) and the integration tests
  (`tests/integration/test_lti_launch.py`).

- `tests/test_lti_service.py` — `process_launch` unit
  tests. 14 cases: learner launch, instructor launch,
  admin-role promotion (one-way), unknown policy,
  retired policy, two consecutive launches (upsert
  semantics), `attempt_reference` is a UUID4, session
  token round-trips, redirect URL uses exam client for
  learner / admin surface for instructor, learner
  launch does not create an `AdminUser`, failed
  launch rolls back participant.

- `tests/test_lti_routes.py` — endpoint tests. 17 cases
  covering `/lti/login` happy path, missing params,
  discovery failure, target_link_uri mismatch, and
  `/lti/launch` happy path (learner + instructor),
  replay, expired `exp`, signature from a key not in
  the JWKS, wrong `iss` (issuer_invalid), wrong
  `nonce` (nonce_mismatch), missing `policy_config_name`
  (policy_not_found), wrong `aud` (audience_invalid),
  unknown role URI (claims_invalid).

- `tests/integration/test_lti_launch.py` — same boundary
  cases as the unit tests, but against a real
  PostgreSQL engine. 9 cases verifying the
  database-level invariants: the JSONB
  `PolicyConfig.extra_rules` is not mutated, the
  `accumulated_medium_score` default of `0` is
  preserved, `consent_recorded_at = started_at`, the
  `AdminUser` natural key matches the `Participant`'s,
  the upsert is transactional, the `attempt_reference`
  is a UUID4, and the admin role promotion preserves
  the participant.
