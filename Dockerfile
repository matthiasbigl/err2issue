# syntax=docker/dockerfile:1.7
#
# Multi-stage build with uv. The builder resolves a locked dependency set into a
# self-contained virtualenv; the runtime stage copies only that venv and the
# source, so no build tooling, cache, or lockfile ships in the final image.
#
# The runtime is distroless: Python and the shared libraries it links, no
# shell, no package manager, no pip. A `python:*-slim` runtime carried ~45
# HIGH CVEs in packages this service never runs (util-linux, ncurses, perl,
# systemd libraries, and the vendored dependencies of the base image's pip),
# none with a fix available, and every one of them fails a consumer's
# scanner that does not pass `--ignore-unfixed`.

# ---- builder -------------------------------------------------------------
# Debian's own python3 package, the same one the distroless runtime ships, so
# the venv's interpreter symlink (/usr/bin/python3.13) resolves in both stages.
FROM debian:trixie-slim AS builder

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/bin/python3 \
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
FROM gcr.io/distroless/python3-debian13 AS runtime

LABEL org.opencontainers.image.title="err2issue" \
      org.opencontainers.image.description="OpenTelemetry errors in, deduplicated GitHub issues out." \
      org.opencontainers.image.source="https://github.com/matthiasbigl/err2issue" \
      org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /app

# Run unprivileged. The service needs no filesystem writes and no raw sockets.
# Numeric, because distroless has no useradd; 10001 is the uid the Kubernetes
# example in docs/DEPLOY.md pins with runAsUser.
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv
COPY --from=builder --chown=10001:10001 /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    E2I_HOST=0.0.0.0 \
    E2I_PORT=4318

USER 10001:10001

# 4318 is the OTLP/HTTP default, so a collector needs no port override.
EXPOSE 4318

# Liveness only — readiness is /readyz, which reports configuration validity.
# A container that cannot reach GitHub should leave the load balancer, not
# restart-loop, so the two are deliberately different endpoints. Exec form:
# there is no shell to run a string through.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:4318/healthz', timeout=2).status==200 else 1)"]

# The distroless image sets ENTRYPOINT to python3; reset it to the console script.
ENTRYPOINT ["err2issue"]
