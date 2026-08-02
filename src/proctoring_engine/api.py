"""FastAPI application boundary for the v1 proctoring service.

The app composes three routers on startup, each guarded by a
fail-closed env-var check:

* ``/lti/login`` + ``/lti/launch`` — LTI 1.3 ingestion (turn N + N+1).
  Mounted when the LTI env vars are configured.
* ``/ws`` — authenticated WebSocket telemetry (turn N+2).  Mounted
  whenever the LTI settings are loaded, since the WS handshake
  reuses the same session-token secret.
* ``/sessions/{id}/status``,
  ``/sessions/{id}/flags/{flag_id}/evidence``,
  ``/sessions/{id}/terminate``, and the four ``/admin/*`` routes —
  the API / orchestration layer (turn N+7).  Mounted when **both**
  the LTI settings and the orchestration settings are available,
  and the evidence-store settings can be loaded.
* ``/client/`` — the browser capture client static bundle (turn N+8).
  Mounted when the ``client-dist/`` directory exists on disk
  (built by the Dockerfile's Node.js build stage).

If a layer's env vars are missing the lifespan logs a warning and
serves the rest of the surface.  This is the v1 dev-mode shape: a
developer with no LMS can still run the service and exercise the
data model + the orchestration surface; a developer with an LMS but
no S3 standalone can still launch and start a session.
"""

from __future__ import annotations

import logging
import pathlib
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from proctoring_engine.config import get_settings
from proctoring_engine.evidence import (
    EvidenceStore,
    EvidenceStoreSettings,
    InMemoryEvidenceStore,
    S3EvidenceStore,
    get_evidence_store_settings,
)
from proctoring_engine.lti import (
    InMemoryLaunchStateStore,
    JwksCache,
    LtiSettings,
    OidcDiscoveryCache,
    RedisLaunchStateStore,
    build_lti_router,
    get_lti_settings,
)
from proctoring_engine.lti.routes import _RouterDeps
from proctoring_engine.orchestration import (
    OrchestrationSettings,
    build_orchestration_router,
    get_orchestration_settings,
)
from proctoring_engine.orchestration._routes import _OrchestrationDeps
from proctoring_engine.websocket import (
    TelemetryEventBuffer,
    build_ws_router,
)
from proctoring_engine.websocket.routes import _WsRouterDeps


logger = logging.getLogger(__name__)


# The database session factory. The application lifespan
# installs this when the engine is ready; the LTI router reads
# it from the same holder via ``_RouterDeps.get_db``. A
# default that raises is set so a route built without going
# through the lifespan fails closed with a clear error.
_db_session_factory: Callable[[], Session]


def _unconfigured_session_factory() -> Session:
    raise RuntimeError(
        "DB session factory is not configured; the application "
        "lifespan installs it at startup. If you are building a "
        "test app directly, call install_db_session_factory(...) "
        "before the test client issues any LTI requests."
    )


_db_session_factory = _unconfigured_session_factory


def install_db_session_factory(factory: Callable[[], Session]) -> None:
    """Install the database session factory used by the LTI routes.

    Called by the application lifespan when the engine is ready,
    and by the integration test harness when it builds the
    router directly.
    """

    global _db_session_factory
    _db_session_factory = factory


# The evidence store seam.  The application lifespan installs this
# when the evidence-store settings are loaded; the orchestration
# router reads it from the same holder via ``_OrchestrationDeps``.
# A default that raises is set so a route built without going
# through the lifespan fails closed with a clear error.
_evidence_store: EvidenceStore


def _unconfigured_evidence_store() -> EvidenceStore:  # type: ignore[empty-body]
    raise RuntimeError(
        "Evidence store is not configured; the application "
        "lifespan installs it at startup. If you are building a "
        "test app directly, call install_evidence_store(...) "
        "before the test client issues any evidence-flush requests."
    )


_evidence_store = _unconfigured_evidence_store  # type: ignore[assignment]


def install_evidence_store(store: EvidenceStore) -> None:
    """Install the :class:`EvidenceStore` used by the orchestration layer.

    Called by the application lifespan in production (the lifespan
    constructs an :class:`S3EvidenceStore` from
    :func:`get_evidence_store_settings`); called by the test harness
    when it builds a router with an :class:`InMemoryEvidenceStore`
    directly.
    """

    global _evidence_store
    _evidence_store = store


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire the routers on app startup if the relevant env vars are
    set, and clean up on shutdown.

    Each layer has an independent try/except so a missing
    ``EVIDENCE_STORE_*`` variable does not also disable the LTI
    layer's launch flow (and vice versa).  The env-var checks use
    the ``get_*_settings`` helpers, which raise
    :class:`ValueError` with a clear variable name when something
    is missing or malformed.

    The database engine is not opened by this lifespan — the
    production deploy wires the engine before the app starts
    (``docs/DEPLOYMENT.md``).
    """

    http_client: httpx.AsyncClient | None = None

    # ---- LTI + WebSocket layer -----------------------------------------
    try:
        settings = get_lti_settings()
    except ValueError as exc:
        logger.warning(
            "LTI settings are not configured; /lti/* and /ws routes are unavailable: %s",
            exc,
        )
    else:
        if settings.state_store_backend == "redis":
            # Lazy-import redis so the dependency isn't required when
            # running on the in-memory backend (the dev default).
            try:
                import redis.asyncio as redis_asyncio
            except ImportError as exc:  # pragma: no cover - defensive
                raise RuntimeError(
                    "redis.asyncio is required for the 'redis' launch-state "
                    "backend; install the redis package"
                ) from exc
            redis_client = redis_asyncio.from_url(
                settings.redis_url,
                decode_responses=False,
            )
            state_store = RedisLaunchStateStore(
                client=redis_client,
                ttl_seconds=settings.state_store_ttl_seconds,
            )
            logger.info(
                "LaunchStateStore wired to Redis at %s",
                settings.redis_url,
            )
        else:
            state_store = InMemoryLaunchStateStore(
                ttl_seconds=settings.state_store_ttl_seconds
            )
        jwks_cache = JwksCache()
        discovery_cache = OidcDiscoveryCache()
        http_client = httpx.AsyncClient(
            timeout=settings.oidc_http_timeout_seconds
        )

        def _http_client_factory() -> httpx.AsyncClient:
            return http_client

        deps = _RouterDeps(
            settings=settings,
            state_store=state_store,
            jwks_cache=jwks_cache,
            discovery_cache=discovery_cache,
            http_client_factory=_http_client_factory,
            get_db=_db_session_factory,
        )
        app.include_router(build_lti_router(deps))

        # The TelemetryEventBuffer is process-global for the WebSocket
        # layer so the HTTP polling endpoints can read from it.
        event_buffer = TelemetryEventBuffer(maxlen=4096)
        ws_deps = _WsRouterDeps(
            settings=settings,
            get_db=_db_session_factory,
            event_buffer=event_buffer,
        )
        app.include_router(build_ws_router(ws_deps))

    # ---- Evidence store seam ------------------------------------------
    evidence_settings: EvidenceStoreSettings | None = None
    try:
        evidence_settings = get_evidence_store_settings()
    except ValueError as exc:
        logger.warning(
            "Evidence store settings are not configured; the "
            "evidence-flush route will use an in-memory store "
            "(non-persistent, suitable for dev only): %s",
            exc,
        )

    if evidence_settings is not None:
        try:
            store: EvidenceStore = S3EvidenceStore(evidence_settings)
            install_evidence_store(store)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Failed to construct S3 evidence store; falling "
                "back to in-memory: %s",
                exc,
            )
            install_evidence_store(InMemoryEvidenceStore())
    # If we couldn't load the evidence settings, we still install an
    # in-memory store so dev-mode mounts the route surface.  The
    # store is process-local and resets on restart, which matches
    # the dev-mode contract.
    if _evidence_store is _unconfigured_evidence_store:
        install_evidence_store(InMemoryEvidenceStore())

    # ---- Orchestration layer ------------------------------------------
    try:
        orchestration_settings = get_orchestration_settings()
    except ValueError as exc:
        logger.warning(
            "Orchestration settings are not configured; the "
            "/sessions/{id}/terminate route and the /admin/* "
            "surface are unavailable: %s",
            exc,
        )
    else:
        # The orchestration routes need LTI settings to decode
        # session tokens for the admin role check.  Re-read them
        # here — silently fail closed if they're missing.
        try:
            lti_settings = get_lti_settings()
        except ValueError as exc:
            logger.warning(
                "Orchestration layer cannot start without LTI "
                "settings: %s",
                exc,
            )
        else:
            orch_deps = _OrchestrationDeps(
                settings=orchestration_settings,
                lti_settings=lti_settings,
                get_db=_db_session_factory,
                evidence_store=_evidence_store,
            )
            app.include_router(build_orchestration_router(orch_deps))

    # ---- Static client bundle ------------------------------------------
    # The browser capture client is built by the Dockerfile's Node.js
    # stage and placed in ``client-dist/``.  In local dev the developer
    # runs ``npm run dev`` from ``client/`` and Vite proxies to the
    # FastAPI backend.  The static mount is a production convenience so
    # the client bundle does not need a separate nginx sidecar or CDN to
    # serve.  If the directory doesn't exist the mount is skipped
    # (fail-open: the API surface is fully functional without the client
    # bundle; the only thing missing is the static HTML/JS).
    client_dist = pathlib.Path("client-dist")
    if client_dist.is_dir():
        app.mount(
            "/client",
            StaticFiles(directory=str(client_dist), html=True),
            name="client",
        )
        logger.info("Mounted client bundle from %s at /client/", client_dist)
    else:
        logger.info(
            "No client-dist/ directory found; /client/ static mount "
            "skipped (run 'npm run build' in client/ or build the "
            "Docker image to create it)."
        )

    try:
        yield
    finally:
        if http_client is not None:
            await http_client.aclose()


app = FastAPI(
    title="AI Proctoring Engine",
    version="0.1.0",
    description="Auditable v1 proctoring data-model service foundation.",
    lifespan=_lifespan,
)


@app.get("/healthz", tags=["operations"])
def healthcheck() -> dict[str, str]:
    """Liveness endpoint that does not expose database credentials or state."""

    return {"status": "ok", "environment": get_settings().app_env}
