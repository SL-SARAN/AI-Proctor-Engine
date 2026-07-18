"""FastAPI application boundary for the data-model implementation phase."""

from fastapi import FastAPI

from proctoring_engine.config import get_settings


app = FastAPI(
    title="AI Proctoring Engine",
    version="0.1.0",
    description="Auditable v1 proctoring data-model service foundation.",
)


@app.get("/healthz", tags=["operations"])
def healthcheck() -> dict[str, str]:
    """Liveness endpoint that does not expose database credentials or state."""

    return {"status": "ok", "environment": get_settings().app_env}

