# Deployment

This document is the operational counterpart to `ARCHITECTURE.md`. It
records the production deployment topology, the local development
environment, the secrets model, and the scaling story — all of which are
in scope now that the data-model layer is reconciled and the
PostgreSQL integration test is in place.

## 1. Topology

```
                     ┌───────────────────────┐
                     │  Cloudflare (TLS +    │
                     │  DDoS, edge routing)  │
                     └───────────┬───────────┘
                                 │
                                 ▼
                     ┌───────────────────────┐
                     │  Kubernetes cluster   │
                     │  (api + worker tiers, │
                     │   autoscaled, single  │
                     │   region multi-AZ)    │
                     └─────┬───────────┬─────┘
                           │           │
              ┌────────────┘           └────────────┐
              ▼                                    ▼
    ┌────────────────────┐              ┌────────────────────┐
    │  Managed Postgres  │              │  Cloudflare R2     │
    │  (RDS / Cloud SQL  │              │  (S3-compatible    │
    │  / Neon)           │              │  evidence storage)  │
    └────────────────────┘              └────────────────────┘
```

The five pieces, in order of operational priority:

1. **Cloudflare** in front of the cluster terminates TLS, provides DDoS
   mitigation, and routes traffic. Cloudflare's R2 product is the
   S3-compatible object storage backend.
2. **Kubernetes** runs two stateless tiers — the API (FastAPI via
   uvicorn) and the worker (async inference consumers). Both are
   horizontally autoscaled. State lives in Postgres and R2; the pods are
   ephemeral.
3. **Managed Postgres** holds the relational audit trail. The
   `termination_record_immutable` and `flag_immutable` triggers are
   applied at migration time and verified by the integration test suite
   in CI.
4. **Cloudflare R2** holds the evidence blobs (video clips, audio
   chunks, image frames). The spec's "S3-compatible" requirement is
   satisfied by R2; the `boto3` client works unchanged.
5. **The inference worker tier** consumes heavy-modality jobs from the
   API tier's task queue. The actual implementation is a future layer;
   the manifest under `k8s/04-deployment-worker.yaml` reserves the slot.

## 2. Local development

`docker compose up --build` brings up the full local stack:

- `postgres` — PostgreSQL 15 with the v1 schema.
- `minio` — S3-compatible object storage; local stand-in for R2.
- `app` — the FastAPI service, hot-reload on source edits.

The compose file is documented inline; every env var that the API
reads is set explicitly so there is no hidden inheritance from
`localhost`. To apply migrations as a one-off:

```sh
docker compose run --rm migrate
```

To run the unit + integration tests locally, set the integration
database URL and run pytest:

```sh
export INTEGRATION_DATABASE_URL="postgresql+psycopg://proctoring:proctoring@127.0.0.1:5432/proctoring"
pytest tests
```

When `INTEGRATION_DATABASE_URL` is unset, the integration suite is
skipped — the SQLite unit suite runs unchanged. This is the same
behavior the CI workflow relies on.

## 3. Sizing for "a few thousand users, easily scalable larger"

For a workload in the few-thousand-concurrent-session range, the
following sizing is sufficient. It is the **floor**, not the ceiling —
the autoscalers in `k8s/08-hpa-api.yaml` and `k8s/09-hpa-worker.yaml`
will scale each tier up to 10 replicas on CPU pressure, and the cluster
can be grown independently.

| Component | Initial sizing | Notes |
|---|---|---|
| API replicas | 2 (HPA up to 10) | WebSocket affinity via sticky LB. |
| Worker replicas | 2 (HPA up to 10) | Scales on CPU; tied to frame-arrival rate. |
| API request/limit | 250m / 2 CPU, 512Mi / 1Gi memory | WebSocket-heavy. |
| Worker request/limit | 500m / 4 CPU, 1Gi / 4Gi memory | ML inference is memory-bound. |
| Postgres | Managed, 2 vCPU / 4 GiB (smallest RDS / Cloud SQL / Neon) | Burstable is fine at this scale. |
| R2 | Per-GB storage, no egress fees | Lifecycle rules drive `retention_expires_at` enforcement. |

If concurrent session count grows past ~5,000, the first thing to
revisit is the **inference worker concurrency per pod** (the
`WORKER_CONCURRENCY` env var) and the **frame-decimation rate** in the
preprocessing layer. Doubling concurrency doubles CPU and memory
pressure per pod, which is the leading indicator that the worker tier
needs to scale out before the API tier does.

## 4. Secrets model

The repository never contains a real secret. The `k8s/02-secret.yaml`
file documents the **shape** of every secret the application needs,
with `REPLACE_ME` placeholders. In production:

- **Postgres credentials** — generated at database creation in the
  managed-Postgres console, stored in the cloud provider's secret
  store (AWS Secrets Manager, GCP Secret Manager), synced into the
  cluster's `proctoring-engine-secret` Secret via External Secrets
  Operator or a sealed-secrets workflow.
- **R2 access keys** — created in the Cloudflare dashboard, scoped to
  one bucket, rotated on a schedule. The same secret-store pattern
  applies.
- **LTI tool private key** — generated once at tool registration,
  stored in the secret store. The platform's public keys are fetched
  from the platform's JWKS endpoint at launch-validation time.
- **LTI tool client ID** (`LTI_TOOL_CLIENT_ID`) — the value the
  platform registers for this tool; sent as the `aud` claim and
  the `client_id` query parameter on every launch. Per-platform
  in a multi-LMS deployment; identical to the value the tool
  returns in the OIDC authorization request.
- **LTI launch URL** (`LTI_LAUNCH_URL`) — the absolute URL the
  platform redirects the browser to after the platform
  authenticates the user; the tool's OIDC `redirect_uri`. Must
  match the URL registered with the platform's LTI 1.3 tool
  registry to the byte. Defaults to
  `http://localhost:8000/lti/launch` for local dev; overridden
  per environment.
- **Session-token secret** (`SESSION_TOKEN_SECRET`) — the HS256
  key used to sign the short-lived session token the tool issues
  on a successful LTI launch. Must be at least 32 bytes of
  cryptographically random material (`openssl rand -base64 48`
  is fine). Rotated quarterly; on rotation, all live sessions
  are invalidated (the WebSocket client re-launches from the
  LMS, which is the documented recovery path).
- **Internal terminate-token** — a shared secret between the API and
  the worker, used only for the internal `/sessions/{id}/terminate`
  call. Rotated quarterly. This is the one credential that is not
  derived from an external system; it exists because the
  fusion-engine-to-orchestration call is service-to-service and must
  not be reachable via an LTI-derived token.
- **Exam client URL** (`EXAM_CLIENT_URL`) — the URL the launch
  handler 302-redirects a learner to, with `?session_token=...`.
  Defaults to `http://localhost:5173/exam` for local dev;
  overridden per environment. **Must be HTTPS in production.**
- **Admin surface URL** (`ADMIN_SURFACE_URL`) — the URL the
  launch handler 302-redirects a non-learner
  (instructor / admin / proctor) to, with `?session_token=...`.
  Defaults to `http://localhost:5173/admin` for local dev;
  overridden per environment. **Must be HTTPS in production.**
- **OIDC HTTP timeout** (`OIDC_HTTP_TIMEOUT_SECONDS`) — the
  timeout the production `httpx.AsyncClient` uses for OIDC
  discovery and JWKS fetches. Defaults to 5.0 seconds; the
  default is suitable for a stable LMS on a low-latency link.

## 5. CI/CD

GitHub Actions, in `.github/workflows/ci.yml`:

- `unit` job — runs on every push and PR, on Python 3.11 and 3.12.
  Installs the package, compiles the source, compiles the Alembic
  migration SQL without applying it, and runs the unit test suite
  against SQLite. Fast feedback loop, no external services required.
- `integration` job — depends on `unit`. Boots a `postgres:15-alpine`
  service container, applies the full migration chain, and runs the
  integration test suite against the real engine. Exercises the
  `flag_immutable` and `termination_record_immutable` triggers under
  direct SQL, verifies the enum values added by the
  audit-reconciliation migration, and round-trips the new columns.
- `build` job — depends on `unit`. Builds the Docker image as a
  smoke test. The image is not pushed; the deployment pipeline is
  owned by the cluster's GitOps workflow (Argo CD / Flux), not by
  GitHub Actions.

## 6. Scaling story (when you outgrow the small cluster)

The architecture separates stateful from stateless cleanly, which is
the property that lets the next round of scaling happen without
redesign:

1. **WebSocket affinity breaks down** past ~10 API replicas on a
   single ingress. The fix is to either (a) move to a stateful
   WebSocket gateway that routes by session ID, or (b) move the
   WebSocket layer to a managed product (Cloudflare Durable Objects,
   Ably, Pusher). Neither is needed yet.
2. **The LTI launch-state store is process-local.** v1's
   `LaunchStateStore` lives in the API pod's memory (see
   `src/proctoring_engine/lti/state.py`). The launch routes are
   single-shot, so a pending `state`/`nonce` value is only ever
   read by the same replica that issued it. This is fine while
   the API tier is pinned to one replica (the v1 default; the
   WebSocket affinity in §6.1 is the larger constraint), but
   becomes a correctness issue the moment a second API replica
   is added. The fix is a Redis-backed implementation of the
   same interface — a configuration swap, not a code rewrite,
   because the route handler and the launch service depend on
   the abstract store.
3. **Postgres becomes the bottleneck** past ~10k concurrent sessions
   with the current write pattern (heavy at flag time, light
   otherwise). The mitigations, in order: (a) add a read replica for
   the review surface; (b) add a connection pooler (PgBouncer) in
   front of the managed instance; (c) shard by `lti_issuer` if
   multi-tenancy demands it.
4. **R2 lifecycle rules** are how `retention_expires_at` actually
   means something. Set a bucket-level lifecycle policy that mirrors
   the per-row `retention_expires_at` to delete objects past their
   retention date. The application-level deletion worker in the
   evidence layer (a future layer) is the in-DB audit of that
   deletion; the lifecycle rule is the storage-side guarantee.

## 7. What this layer does not yet include

- **Inference worker implementation.** The deployment exists, the
  pod runs, the actual queue consumer is a future layer.
- **Live WebSocket session broker.** The ingress timeout is set to
  one hour; longer sessions need the gateway pattern in §6.1.
- **Retention deletion worker.** The schema records
  `retention_expires_at`; the job that acts on it is a future layer
  in the evidence store.
- **Observability.** Prometheus metrics endpoint, structured
  logging, and tracing are stubbed in the manifest slots but not yet
  implemented in code.

Each of these is the next unchecked item in `SYSTEM_STATE.md` §12.
