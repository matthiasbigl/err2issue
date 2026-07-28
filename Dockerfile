# syntax=docker/dockerfile:1.7
#
# Multi-stage build with uv. The builder resolves a locked dependency set into a
# self-contained virtualenv; the runtime stage copies only that venv and the
# source, so no build tooling, cache, or lockfile ships in the final image.

# ---- builder -------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve from the lockfile alone. Kept in its own layer so a
# source-only change does not re-resolve or re-download anything.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- runtime -------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

LABEL org.opencontainers.image.title="err2issue" \
      org.opencontainers.image.description="OpenTelemetry errors in, deduplicated GitHub issues out." \
      org.opencontainers.image.source="https://github.com/matthiasbigl/err2issue" \
      org.opencontainers.image.licenses="Apache-2.0"

# Run unprivileged. The service needs no filesystem writes and no raw sockets.
RUN groupadd --system --gid 10001 err2issue \
 && useradd --system --uid 10001 --gid err2issue --no-create-home err2issue

WORKDIR /app

COPY --from=builder --chown=err2issue:err2issue /app/.venv /app/.venv
COPY --from=builder --chown=err2issue:err2issue /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    E2I_HOST=0.0.0.0 \
    E2I_PORT=4318

USER err2issue

# 4318 is the OTLP/HTTP default, so a collector needs no port override.
EXPOSE 4318

# Liveness only — readiness is /readyz, which reports configuration validity.
# A container that cannot reach GitHub should leave the load balancer, not
# restart-loop, so the two are deliberately different endpoints.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4318/healthz', timeout=2).status==200 else 1)"

ENTRYPOINT ["err2issue"]
