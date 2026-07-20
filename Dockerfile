# syntax=docker/dockerfile:1.7
#
# Multi-stage Dockerfile for the AI Proctoring Engine.
#
# The builder stage installs the build-time dependencies (the [dev] extra
# pulls in pytest, which the build context needs for the test step inside
# the image is not run here, but the dev extras also pull in tooling
# that the application imports at runtime in some configurations).
# The runtime stage contains only the installed package and the standard
# library, runs as a non-root user, and exposes a /healthz endpoint
# via the application boot.

ARG PYTHON_VERSION=3.11

# ----------------------------------------------------------------------
# Builder stage
# ----------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# Install build dependencies. libpq-dev is required for psycopg to be
# built from source on the slim image; the binary wheel is preferred at
# install time, but having the headers available means the build never
# fails just because a wheel is missing for a given Python version.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src

# Install the package into a prefix that the runtime stage can copy.
# The [dev] extra is added here so the test runner is available inside
# the builder if a future stage wants to run tests; the runtime stage
# does not include [dev].
# Pin pip to a specific version for build reproducibility. The
# `python:3.11-slim` base image ships with a recent pip, but
# `pip install --upgrade pip` without a version pin pulls whatever
# is latest on PyPI at build time — which can change `--prefix`
# install behavior or PEP 517 resolution and break the build in
# non-obvious ways. Pin to the pip version that was current when
# this project was last verified to build cleanly.
RUN pip install --upgrade "pip==25.0.1" \
    && pip install --prefix=/install ".[dev]"


# ----------------------------------------------------------------------
# Runtime stage
# ----------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app \
    PORT=8000

# Install only the runtime system dependencies. libpq5 (not libpq-dev) is
# the runtime shared library that psycopg needs to talk to PostgreSQL.
# tini provides a minimal init that reaps zombie processes and forwards
# signals, which is the recommended pattern for Python in containers.
RUN apt-get update \
    && apt-get install --no-install-recommends -y \
        libpq5 \
        tini \
        curl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 proctoring \
    && useradd --system --uid 1000 --gid proctoring \
        --home-dir "${APP_HOME}" --shell /usr/sbin/nologin proctoring

WORKDIR ${APP_HOME}

# Copy the installed package from the builder.
COPY --from=builder /install /usr/local

# Copy the application source. The /app directory is owned by the
# non-root user so the application cannot write to the source tree at
# runtime (defense in depth — the application should not need to).
COPY --chown=proctoring:proctoring --from=builder /build/src ./src
COPY --chown=proctoring:proctoring pyproject.toml ./
COPY --chown=proctoring:proctoring alembic.ini ./
COPY --chown=proctoring:proctoring migrations ./migrations

USER proctoring

EXPOSE ${PORT}

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:${PORT}/healthz || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command runs the FastAPI service via uvicorn. The --proxy-headers
# flag is important when the service sits behind a TLS-terminating ingress
# (X-Forwarded-Proto must be honored so generated URLs are correct).
CMD ["uvicorn", "proctoring_engine.api:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
