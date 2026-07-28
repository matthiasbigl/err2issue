"""HTTP surface: an OTLP/HTTP logs receiver plus operational endpoints.

Filing is done off the request path. A collector's exporter expects a prompt
response; issuing three GitHub round-trips and an LLM call inline would blow
past its timeout and trigger a retry, which means duplicated work for every
error. So `/v1/logs` decodes, enqueues, and returns immediately.

When the queue is full we shed load and say so *in the protocol* via OTLP's
`partial_success.rejected_log_records`, rather than silently dropping. The
collector's own retry queue then absorbs the overflow — which is exactly the
failure mode PLAN.md §7 wants: err2issue can degrade without ever affecting
the applications producing the telemetry.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response

from . import otlp
from .ai import Enricher
from .config import Settings, get_settings
from .context import TraceBuffer
from .github.auth import build_provider
from .github.client import GitHubClient
from .pipeline import Pipeline
from .redact import Redactor
from .routing import Router
from .sinks import build_sink
from .suppress import Suppressor

log = logging.getLogger(__name__)

QUEUE_MAXSIZE = 1000
WORKER_COUNT = 4


class ConfigurationError(RuntimeError):
    """Raised at startup when settings cannot produce a working service."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        joined = "\n  - ".join(problems)
        super().__init__(
            f"err2issue cannot start with this configuration:\n  - {joined}\n"
            "Fix the environment, or set E2I_SINK=dry-run to run without GitHub."
        )


class Service:
    """Owns the pipeline, the work queue, and the worker tasks."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self.workers: list[asyncio.Task] = []
        self.pipeline: Pipeline | None = None
        self.http: httpx.AsyncClient | None = None
        self.startup_errors: list[str] = settings.validation_errors()
        self.dropped_backpressure = 0

    async def start(self) -> None:
        settings = self.settings

        # Fail fast: refuse to serve with a configuration that cannot work.
        # Accepting telemetry we are guaranteed to drop is worse than not
        # binding the port, because the collector's retry queue would absorb
        # the outage silently and the operator would learn nothing.
        if self.startup_errors:
            raise ConfigurationError(self.startup_errors)

        self.http = httpx.AsyncClient(timeout=20.0)

        provider = build_provider(settings, client=self.http)
        client = (
            GitHubClient(settings.github_api_url, provider, client=self.http) if provider else None
        )
        sink = build_sink(settings, client)

        self.pipeline = Pipeline(
            sink=sink,
            router=Router.from_spec(settings.route_map, settings.github_repo),
            suppressor=Suppressor(
                window_seconds=settings.suppress_window_seconds,
                max_per_minute=settings.max_dispatches_per_minute,
                max_new_per_day=settings.max_new_fingerprints_per_day,
            ),
            redactor=Redactor(settings.redact, settings.redact_patterns),
            enricher=Enricher(
                api_key=settings.anthropic_api_key,
                model=settings.ai_model,
                timeout=settings.ai_timeout_seconds,
                enabled=settings.ai_enabled,
            ),
            trace_buffer=TraceBuffer(max_traces=settings.trace_buffer_size),
            max_context_log_lines=settings.max_context_log_lines,
            drop_unrouted=settings.drop_unrouted,
        )
        self.workers = [asyncio.create_task(self._worker(i)) for i in range(WORKER_COUNT)]
        log.info(
            "err2issue ready: sink=%s auth=%s routes=%s",
            sink.name,
            provider.describe() if provider else "none",
            self.pipeline.router.describe(),
        )

    async def _worker(self, index: int) -> None:
        assert self.pipeline is not None
        while True:
            event = await self.queue.get()
            try:
                await self.pipeline.handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker %d: unhandled error while filing", index)
            finally:
                self.queue.task_done()

    async def stop(self) -> None:
        # Give in-flight work a moment to finish before tearing down.
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.queue.join(), timeout=10.0)
        for task in self.workers:
            task.cancel()
        for task in self.workers:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self.pipeline:
            await self.pipeline.aclose()
        if self.http:
            await self.http.aclose()

    def enqueue(self, event) -> bool:
        try:
            self.queue.put_nowait(event)
            return True
        except asyncio.QueueFull:
            self.dropped_backpressure += 1
            return False

    @property
    def ready(self) -> bool:
        return self.pipeline is not None and not self.startup_errors


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        service = Service(settings)
        app.state.service = service
        await service.start()  # raises ConfigurationError rather than serving broken
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(
        title="err2issue",
        version="0.1.0",
        summary="OpenTelemetry errors in, deduplicated GitHub issues out.",
        lifespan=lifespan,
    )

    @app.post("/v1/logs")
    async def receive_logs(request: Request) -> Response:
        service: Service = request.app.state.service
        content_type = (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        encoding = (request.headers.get("content-encoding") or "").strip().lower()

        try:
            body = await _read_body(request)
        except _BodyTooLarge as exc:
            return _error(413, str(exc), content_type)

        try:
            body = _decompress(body, encoding)
        except ValueError as exc:
            return _error(400, str(exc), content_type)

        try:
            if content_type in otlp.PROTOBUF_CONTENT_TYPES:
                decoded = otlp.decode_protobuf(body)
            elif content_type in otlp.JSON_CONTENT_TYPES or not content_type:
                decoded = otlp.decode_json(json.loads(body or b"{}"))
            else:
                return _error(415, f"unsupported content-type {content_type!r}", content_type)
        except (otlp.DecodeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            return _error(400, str(exc), content_type)

        service_metrics = service.pipeline.metrics if service.pipeline else None
        if service_metrics:
            service_metrics.received_records += len(decoded)

        events, lines = otlp.to_events(
            decoded, require_exception=settings.require_exception_attributes
        )

        if service.pipeline:
            trace_ids = [
                record["trace_id"]
                for _, record in decoded
                if not otlp.is_error(record, settings.require_exception_attributes)
                and (record.get("body") or "").strip()
            ]
            service.pipeline.absorb_context(lines, trace_ids)

        rejected = sum(0 if service.enqueue(event) else 1 for event in events)
        return _ok(content_type, rejected)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness: the process is up. Never depends on GitHub."""
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> Response:
        """Readiness: configuration is valid and the pipeline is built.

        Split from /healthz deliberately — a container that cannot reach GitHub
        should be pulled from rotation, not restart-looped (CHALLENGE.md §8).
        """
        service: Service = request.app.state.service
        payload: dict[str, Any] = {
            "ready": service.ready,
            "sink": settings.sink,
            "auth": settings.auth_mode,
            "problems": service.startup_errors,
        }
        return Response(
            content=json.dumps(payload),
            status_code=200 if service.ready else 503,
            media_type="application/json",
        )

    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        service: Service = request.app.state.service
        if not service.pipeline:
            return Response(content="", media_type="text/plain; version=0.0.4")
        text = service.pipeline.metrics.as_prometheus()
        text += (
            "# HELP err2issue_dropped_backpressure_total Events dropped because "
            "the work queue was full.\n"
            "# TYPE err2issue_dropped_backpressure_total counter\n"
            f"err2issue_dropped_backpressure_total {service.dropped_backpressure}\n"
        )
        # A repository going unavailable drops every error routed to it while
        # /readyz stays green — deliberately, since the failure is per-repo and
        # restarting the pod does not fix it. These are the only signals that
        # say it is happening, so they are worth alerting on.
        health = service.pipeline.sink.health()
        if "unavailable_repos" in health:
            text += (
                "# HELP err2issue_repos_unavailable Repositories currently in "
                "the unavailable cooldown, dropping their errors.\n"
                "# TYPE err2issue_repos_unavailable gauge\n"
                f"err2issue_repos_unavailable {len(health['unavailable_repos'])}\n"
                "# HELP err2issue_repo_unavailable_total Times a repository was "
                "marked unavailable.\n"
                "# TYPE err2issue_repo_unavailable_total counter\n"
                f"err2issue_repo_unavailable_total {health['unavailable_events']}\n"
            )
        return Response(content=text, media_type="text/plain; version=0.0.4")

    @app.get("/stats")
    async def stats(request: Request) -> dict[str, Any]:
        """Human-readable counterpart to /metrics, for debugging a deployment."""
        service: Service = request.app.state.service
        if not service.pipeline:
            return {"ready": False}
        return {
            "ready": service.ready,
            "sink": settings.sink,
            "auth": settings.auth_mode,
            "queue_depth": service.queue.qsize(),
            "dropped_backpressure": service.dropped_backpressure,
            "routing": service.pipeline.router.describe(),
            "suppression": service.pipeline.suppressor.stats(),
            "traces_buffered": len(service.pipeline.traces),
            "metrics": service.pipeline.metrics.as_dict(),
            "sink_health": service.pipeline.sink.health(),
        }

    return app


# -- request decoding ------------------------------------------------------

# The OTLP/HTTP spec allows gzip, and the collector's otlphttp exporter enables
# it by default — so in practice most real traffic arrives compressed. ASGI
# servers do not decompress request bodies, so without this every export from a
# default-configured collector fails to parse and 400s. Found by running a real
# collector against the service, not by reading the spec.
_MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024


class _BodyTooLarge(Exception):
    """The request body exceeded the ingest ceiling before it was even decoded."""


async def _read_body(request: Request) -> bytes:
    """Read the body, refusing anything past the post-decompression ceiling.

    `await request.body()` buffers the whole request with no bound, so an
    uncompressed payload was limited only by the container's memory limit —
    while a *compressed* one was capped at 64MB by `_bounded`. The uncompressed
    path is the easier one to send, so it needs the same ceiling. Streaming
    means an oversized body is refused partway through rather than after it has
    all been buffered.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_DECOMPRESSED_BYTES:
        raise _BodyTooLarge(
            f"body of {declared} bytes exceeds {_MAX_DECOMPRESSED_BYTES}; refusing to read"
        )

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _MAX_DECOMPRESSED_BYTES:
            raise _BodyTooLarge(f"body exceeds {_MAX_DECOMPRESSED_BYTES} bytes; refusing to read")
        chunks.append(chunk)
    return b"".join(chunks)


def _decompress(body: bytes, encoding: str) -> bytes:
    if not encoding or encoding == "identity":
        return body

    if encoding == "gzip":
        import gzip
        import zlib

        try:
            return _bounded(gzip.decompress(body))
        except (OSError, zlib.error) as exc:
            raise ValueError(f"body is not valid gzip: {exc}") from exc

    if encoding in ("deflate", "zlib"):
        import zlib

        try:
            return _bounded(zlib.decompress(body))
        except zlib.error:
            try:  # raw deflate, no zlib header
                return _bounded(zlib.decompress(body, -zlib.MAX_WBITS))
            except zlib.error as exc:
                raise ValueError(f"body is not valid deflate: {exc}") from exc

    raise ValueError(f"unsupported content-encoding {encoding!r}")


def _bounded(data: bytes) -> bytes:
    """Guard against a decompression bomb from a misconfigured or hostile sender."""
    if len(data) > _MAX_DECOMPRESSED_BYTES:
        raise ValueError(
            f"decompressed body exceeds {_MAX_DECOMPRESSED_BYTES} bytes; refusing to parse"
        )
    return data


# -- OTLP response helpers -------------------------------------------------


def _ok(content_type: str, rejected: int) -> Response:
    """Success is 200 with an ExportLogsServiceResponse.

    Shedding is reported via partial_success so the collector learns about it
    through the protocol rather than by inference.
    """
    if content_type in otlp.PROTOBUF_CONTENT_TYPES:
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
            ExportLogsServiceResponse,
        )

        response = ExportLogsServiceResponse()
        if rejected:
            response.partial_success.rejected_log_records = rejected
            response.partial_success.error_message = "err2issue queue full; records shed"
        return Response(content=response.SerializeToString(), media_type="application/x-protobuf")

    payload: dict[str, Any] = {}
    if rejected:
        payload = {
            "partialSuccess": {
                "rejectedLogRecords": rejected,
                "errorMessage": "err2issue queue full; records shed",
            }
        }
    return Response(content=json.dumps(payload), media_type="application/json")


def _error(status: int, message: str, content_type: str) -> Response:
    return Response(
        content=json.dumps({"error": message}),
        status_code=status,
        media_type="application/json",
    )


app = create_app
