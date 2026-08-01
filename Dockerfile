# syntax=docker/dockerfile:1
FROM python:3.12-slim

# uv gives a fast, lockfile-exact install with no resolution at build time.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    FF_TRANSPORT=http \
    FF_CACHE_DIR=/data/cache

WORKDIR /app

# Dependencies only. --no-install-project keeps the build independent of the
# project metadata (README, version), so a docs change does not invalidate the
# layer and a missing README cannot fail the build. PYTHONPATH above makes
# `python -m ff_assist.server` work without installing the package itself.
COPY pyproject.toml uv.lock* ./
RUN uv sync --extra mcp --no-install-project --frozen \
    || uv sync --extra mcp --no-install-project

COPY src/ ./src/
# Diagnostics ship with the image. They are a few KB of text, and the whole
# point of check_external.py is running it *on this host* — the deployed
# network is the one scheduled tasks use, and it can differ from a laptop's.
COPY scripts/ ./scripts/

# Secrets never enter the image — they arrive as env vars at runtime via
# `fly secrets set`, and .dockerignore keeps .env out of the build context.
RUN mkdir -p /data/cache \
    && useradd -m -u 10001 ff \
    && chown -R ff /data /app
USER ff

EXPOSE 8000
CMD [".venv/bin/python", "-m", "ff_assist.server", "--http"]
