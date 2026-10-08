# syntax=docker/dockerfile:1.7
#
# Multi-stage build with uv. The builder resolves a locked dependency set into a
# self-contained virtualenv; the runtime stage copies only that venv, the
# interpreter it runs on, and the source, so no build tooling, cache, or
# lockfile ships in the final image.
#
# The runtime is distroless/cc: glibc, libssl and CA certificates, nothing
# else. No shell, no package manager, no pip, and no distribution Python. The
# interpreter is uv's standalone CPython build, which tracks upstream patch
# releases; Debian's python3 lagged several and carried unfixed HIGH CVEs, as
# did util-linux, ncurses, perl and the slim image's pip, all of which an image
# scanner that does not pass `--ignore-unfixed` rejects. See docs/GOTCHAS.md.

# ---- builder -------------------------------------------------------------
FROM debian:trixie-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/

# The interpreter lives at the same path in both stages: the venv's bin/python
# is a symlink to it, so a different path in the runtime would not start.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON=3.13

# The standalone build bundles pip, whose vendored urllib3, msgpack and
# setuptools image scanners flag through pip's own SBOM. Nothing here runs pip
# (uv installs the venv), so it does not ship.
RUN uv python install 3.13 \
 && rm -rf /opt/python/*/bin/pip* \
           /opt/python/*/lib/python3.*/site-packages/pip \
           /opt/python/*/lib/python3.*/site-packages/pip-*.dist-info \
           /opt/python/*/lib/python3.*/ensurepip

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
FROM gcr.io/distroless/cc-debian13 AS runtime

LABEL org.opencontainers.image.title="err2issue" \
      org.opencontainers.image.description="OpenTelemetry errors in, deduplicated GitHub issues out." \
      org.opencontainers.image.source="https://github.com/matthiasbigl/err2issue" \
      org.opencontainers.image.licenses="Apache-2.0"

WORKDIR /app

COPY --from=builder /opt/python /opt/python
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

ENTRYPOINT ["err2issue"]
