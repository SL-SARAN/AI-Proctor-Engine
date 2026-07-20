"""FastAPI application boundary for the v1 proctoring service.

The app composes the LTI 1.3 ingestion layer (``/lti/login`` and
``/lti/launch``) with the liveness probe at ``/healthz``. The LTI
router is built lazily on startup: if the LTI env vars are set,
the production services (``LaunchStateStore``, ``JwksCache``,
``OidcDiscoveryCache``, a shared ``httpx.AsyncClient``) are
constructed and the routes are mounted. If not, the launch flow
is unavailable and only ``/healthz`` is served — this is the
shape the v1 integration test suite expects, where a Postgres
fixture plus a ``TestClient`` is enough to exercise the data
model without the LTI setup.

Future layers (the WebSocket handshake, the admin surface, the
proctor review queue, etc.) will be mounted in the same
lifespan-managed path.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

import httpx
from fastapi import FastAPI
from sqlalchemy.orm import Session

from proctoring_engine.config import get_settings
from proctoring_engine.lti import (
    JwksCache,
    LaunchStateStore,
    LtiSettings,
    OidcDiscoveryCache,
    build_lti_router,
    get_lti_settings,
)
from proctoring_engine.lti.routes import _RouterDeps


logger = logging.getLogger(__name__)


# The database session factory. The application lifespan
# installs this when the engine is ready; the LTI router reads
# it from the same holder via ``_RouterDeps.get_db``. A
# default that raises is set so a route built without going
# through the lifespan fails closed with a clear error.
_db_session_factory: Callable[[], Session] = _unconfigured_session_factory


def install_db_session_factory(factory: Callable[[], Session]) -> None:
    """Install the database session factory used by the LTI routes.

    Called by the application lifespan when the engine is ready,
    and by the integration test harness when it builds the
    router directly.
    """

    global _db_session_factory
    _db_session_factory = factory


def _unconfigured_session_factory() -> Session:
    raise RuntimeError(
        "DB session factory is not configured; the application "
        "lifespan installs it at startup. If you are building a "
        "test app directly, call install_db_session_factory(...) "
        "before the test client issues any LTI requests."
    )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire the LTI router on app startup if the LTI env vars
    are set, and clean up on shutdown.

    The env-var check uses :func:`get_lti_settings`, which
    raises :class:`ValueError` if a required variable is
    missing. The lifespan catches the error, logs a warning,
    and serves only ``/healthz``. This is the v1 dev-mode
    shape: a developer with no LMS can still run the service
    and exercise the data model.

    The database engine is not opened by this lifespan — the
    v1 service defers that to the orchestration layer
    (``docs/07-api-orchestration-design.md``). For the
    current layer, the LTI launch only needs a session; the
    production deploy wires the engine before the app starts.
    """

    try:
        settings = get_lti_settings()
    except ValueError as exc:
        logger.warning(
            "LTI settings are not configured; /lti/* routes are unavailable: %s",
            exc,
        )
        yield
        return

    state_store = LaunchStateStore(ttl_seconds=settings.state_store_ttl_seconds)
    jwks_cache = JwksCache()
    discovery_cache = OidcDiscoveryCache()
    http_client = httpx.AsyncClient(timeout=settings.oidc_http_timeout_seconds)

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

    try:
        yield
    finally:
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
