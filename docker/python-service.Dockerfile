# syntax=docker/dockerfile:1.7
# =============================================================================
# Shared multi-stage build for every Python service in the monorepo.
#   docker build -f docker/python-service.Dockerfile --build-arg SERVICE=checkout .
#
# Stage 1 (uv)      : the uv binary only. Never reaches the final image.
# Stage 2 (builder) : resolves & installs deps into /opt/venv from uv.lock.
#                     Third-party deps are a separate cached layer from our code,
#                     so code edits don't reinstall dependencies.
# Stage 3 (runtime) : clean slim base + /opt/venv + non-root user.
#                     No uv, no compilers, no caches, no source tree, no tests.
#
# Base is python:*-slim (glibc), not alpine (musl): see docs/SYSTEM_DESIGN.md §14.3.
# =============================================================================
ARG PYTHON_BASE_IMAGE=python:3.13-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.18

FROM ${UV_IMAGE} AS uv

# -----------------------------------------------------------------------------
FROM ${PYTHON_BASE_IMAGE} AS builder
ARG SERVICE
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
COPY --from=uv /uv /usr/local/bin/uv
# If a dependency ever lacks a prebuilt wheel, install build tools HERE only
# (e.g. apt-get install build-essential). They never reach the runtime stage.
WORKDIR /src

# Layer 1: third-party dependencies (cache busts only when uv.lock changes)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-workspace --package "${SERVICE}-svc"

# Layer 2: shared lib + this service's code, installed non-editable into the venv
COPY pyproject.toml uv.lock ./
COPY libs/common libs/common
COPY services/${SERVICE} services/${SERVICE}
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --package "${SERVICE}-svc"

# -----------------------------------------------------------------------------
FROM ${PYTHON_BASE_IMAGE} AS runtime
ARG SERVICE
LABEL org.opencontainers.image.title="${SERVICE}-svc" \
      org.opencontainers.image.source="conversational-commerce-agent"
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
# The process to run (api | worker | migrate) is set per container in
# docker-compose.yml, so one image serves all of a service's processes.
