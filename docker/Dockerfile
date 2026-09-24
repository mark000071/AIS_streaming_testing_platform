# One image for every Python service; the compose `command` picks the service.
FROM python:3.11-slim AS base
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/contracts/pyproject.toml packages/contracts/
COPY packages/sdk/pyproject.toml packages/sdk/
COPY packages/core/pyproject.toml packages/core/
COPY packages/predictors/pyproject.toml packages/predictors/
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-workspace

COPY packages packages
COPY config config
COPY fixtures fixtures
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

# Refuse to build an image that cannot reproduce the golden feature sample.
RUN python -m envship_core.features

ENTRYPOINT ["envship"]
