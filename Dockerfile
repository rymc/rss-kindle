# syntax=docker/dockerfile:1

FROM python:3.12-slim-bookworm AS reader

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    HOME=/tmp \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /uvx /bin/
WORKDIR /app
RUN groupadd --system --gid 10001 rsskindle \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin rsskindle
COPY --chown=10001:10001 pyproject.toml uv.lock README.md .env.example source-bridge.example.toml ./
RUN --mount=type=cache,target=/tmp/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY --chown=10001:10001 app ./app
RUN --mount=type=cache,target=/tmp/.cache/uv \
    mkdir -p /app/data \
    && chown -R 10001:10001 /app/data \
    && uv sync --frozen --no-dev
EXPOSE 8000
USER 10001:10001
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM mcr.microsoft.com/playwright/python:v1.58.0-noble AS browser

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    HOME=/tmp \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:0.8.15 /uv /uvx /bin/
WORKDIR /app
RUN groupadd --system --gid 10001 rsskindle \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app --shell /usr/sbin/nologin rsskindle
COPY --chown=10001:10001 pyproject.toml uv.lock README.md .env.example source-bridge.example.toml ./
RUN --mount=type=cache,target=/tmp/.cache/uv \
    uv sync --frozen --no-dev --extra browser --no-install-project
COPY --chown=10001:10001 app ./app
RUN --mount=type=cache,target=/tmp/.cache/uv \
    mkdir -p /app/data \
    && chown -R 10001:10001 /app/data \
    && uv sync --frozen --no-dev --extra browser
EXPOSE 8000 8100
USER 10001:10001
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]

FROM reader AS default
