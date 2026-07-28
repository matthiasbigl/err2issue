"""HTTP surface: the OTLP receiver plus operational endpoints.

Tests run against `dry-run` so nothing touches GitHub. The behaviours that
matter here are protocol-level: correct OTLP responses, correct content-type
negotiation, backpressure signalled through `partial_success`, and a startup
that refuses to serve on a broken configuration.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from err2issue.app import ConfigurationError, Service, _ok, create_app
from err2issue.config import Settings
from tests.conftest import make_event, otlp_json
from tests.test_otlp import build_protobuf


def dry_run_settings(**overrides) -> Settings:
    defaults = dict(sink="dry-run", github_repo="acme/api", ai_enabled=False, redact=True)
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
def client():
    with TestClient(create_app(dry_run_settings())) as test_client:
        yield test_client


def drain(test_client) -> None:
    """Let the background workers finish before asserting on counters.

    Each request yields to the event loop, which is enough for the workers to
    pick the queue up; the loop is bounded so a stuck worker fails the test
    rather than hanging it.
    """
    service = test_client.app.state.service
    for _ in range(200):
        if service.queue.empty():
            return
        test_client.get("/healthz")
    raise AssertionError("work queue did not drain")


# -- OTLP ingest -----------------------------------------------------------


def test_json_export_is_accepted(client):
    response = client.post("/v1/logs", json=otlp_json())
    assert response.status_code == 200
    assert response.json() == {}


def test_protobuf_export_is_accepted(client):
    response = client.post(
        "/v1/logs",
        content=build_protobuf(),
        headers={"content-type": "application/x-protobuf"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-protobuf")


def test_response_encoding_mirrors_the_request_encoding(client):
    json_response = client.post("/v1/logs", json=otlp_json())
    assert json_response.headers["content-type"].startswith("application/json")


def test_protobuf_success_body_is_a_valid_export_response(client):
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse

    response = client.post(
        "/v1/logs", content=build_protobuf(), headers={"content-type": "application/x-protobuf"}
    )
    parsed = ExportLogsServiceResponse()
    parsed.ParseFromString(response.content)
    assert parsed.partial_success.rejected_log_records == 0


def test_alternate_protobuf_content_type_is_accepted(client):
    response = client.post(
        "/v1/logs", content=build_protobuf(), headers={"content-type": "application/protobuf"}
    )
    assert response.status_code == 200


def test_content_type_parameters_are_ignored(client):
    response = client.post(
        "/v1/logs",
        content=json.dumps(otlp_json()),
        headers={"content-type": "application/json; charset=utf-8"},
    )
    assert response.status_code == 200


def test_unsupported_content_type_is_rejected(client):
    response = client.post("/v1/logs", content=b"hello", headers={"content-type": "text/csv"})
    assert response.status_code == 415


def test_malformed_json_returns_400_not_500(client):
    response = client.post(
        "/v1/logs", content=b"{not json", headers={"content-type": "application/json"}
    )
    assert response.status_code == 400
    assert "error" in response.json()


def test_malformed_protobuf_returns_400_not_500(client):
    response = client.post(
        "/v1/logs",
        content=b"\xff\xff\xff\xffgarbage\x00",
        headers={"content-type": "application/x-protobuf"},
    )
    assert response.status_code == 400


def test_empty_body_is_accepted_as_an_empty_export(client):
    response = client.post("/v1/logs", content=b"", headers={"content-type": "application/json"})
    assert response.status_code == 200


def test_non_error_records_are_accepted_but_file_nothing(client):
    response = client.post(
        "/v1/logs",
        json=otlp_json(severity_number=9, exception_type=None, body="just info"),
    )
    assert response.status_code == 200
    drain(client)
    assert client.app.state.service.pipeline.metrics.error_events == 0


def test_an_error_record_reaches_the_sink(client):
    client.post("/v1/logs", json=otlp_json())
    drain(client)
    assert client.app.state.service.pipeline.metrics.error_events == 1


def test_full_queue_sheds_and_counts_instead_of_blocking():
    """Backpressure must never block the collector's export call."""
    service = Service(dry_run_settings())
    for _ in range(service.queue.maxsize):
        assert service.enqueue(make_event()) is True
    assert service.enqueue(make_event()) is False
    assert service.dropped_backpressure == 1


def test_shedding_is_reported_via_otlp_partial_success_json():
    """Signalled in the protocol so the collector can retry, not swallowed."""
    payload = json.loads(_ok("application/json", rejected=3).body)
    assert payload["partialSuccess"]["rejectedLogRecords"] == 3
    assert "err2issue" in payload["partialSuccess"]["errorMessage"]


def test_shedding_is_reported_via_otlp_partial_success_protobuf():
    from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceResponse

    parsed = ExportLogsServiceResponse()
    parsed.ParseFromString(_ok("application/x-protobuf", rejected=2).body)
    assert parsed.partial_success.rejected_log_records == 2


def test_a_clean_export_reports_no_partial_success():
    assert json.loads(_ok("application/json", rejected=0).body) == {}


# -- operational endpoints -------------------------------------------------


def test_healthz_is_liveness_only(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_reports_ready_when_configured(client):
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_metrics_are_prometheus_formatted(client):
    client.post("/v1/logs", json=otlp_json())
    drain(client)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "err2issue_error_events_total" in response.text
    assert "# TYPE" in response.text


def test_stats_exposes_routing_and_suppression_state(client):
    response = client.get("/stats")
    payload = response.json()
    assert payload["sink"] == "dry-run"
    assert payload["routing"]["default"] == "acme/api"
    assert "suppression" in payload


def test_stats_exposes_sink_health(client):
    """A repository dropping every error routed to it is otherwise invisible:
    /readyz stays green and the log line is hours old."""
    assert client.get("/stats").json()["sink_health"] == {}, "dry-run has no repo state"


# -- fail fast -------------------------------------------------------------


def test_missing_credentials_refuse_to_start():
    """Serving with no way to file is worse than not binding the port."""
    settings = Settings(sink="github", github_repo="acme/api", github_token=None)
    with pytest.raises(ConfigurationError, match="credentials"):
        with TestClient(create_app(settings)):
            pass


def test_missing_routing_refuses_to_start():
    settings = Settings(sink="github", github_token="ghp_x", github_repo=None, route_map="")
    with pytest.raises(ConfigurationError, match="routing"):
        with TestClient(create_app(settings)):
            pass


def test_malformed_route_map_refuses_to_start():
    settings = Settings(sink="github", github_token="ghp_x", route_map="bad-entry-no-equals")
    with pytest.raises(ConfigurationError, match="ROUTE_MAP"):
        with TestClient(create_app(settings)):
            pass


def test_malformed_repo_refuses_to_start():
    settings = Settings(sink="github", github_token="ghp_x", github_repo="not-a-repo")
    with pytest.raises(ConfigurationError):
        with TestClient(create_app(settings)):
            pass


def test_bad_custom_redaction_pattern_refuses_to_start():
    settings = Settings(
        sink="dry-run", github_repo="acme/api", redact=True, redact_extra_patterns="([unclosed"
    )
    with pytest.raises(ConfigurationError, match="REDACT"):
        with TestClient(create_app(settings)):
            pass


def test_nonsense_limits_refuse_to_start():
    settings = Settings(sink="dry-run", github_repo="acme/api", suppress_window_seconds=0)
    with pytest.raises(ConfigurationError, match="positive"):
        with TestClient(create_app(settings)):
            pass


def test_invalid_sink_is_rejected_at_settings_construction():
    with pytest.raises(ValueError, match="sink must be one of"):
        Settings(sink="carrier-pigeon")


def test_dry_run_starts_without_any_credentials():
    """The escape hatch: evaluate the pipeline with nothing configured."""
    with TestClient(create_app(Settings(sink="dry-run"))) as test_client:
        assert test_client.get("/healthz").status_code == 200


# -- shutdown --------------------------------------------------------------


def test_queued_work_is_drained_on_shutdown():
    app = create_app(dry_run_settings())
    with TestClient(app) as test_client:
        for _ in range(5):
            test_client.post("/v1/logs", json=otlp_json())
        service = test_client.app.state.service
    assert service.queue.empty()


def test_workers_are_cancelled_on_shutdown():
    app = create_app(dry_run_settings())
    with TestClient(app) as test_client:
        service = test_client.app.state.service
    assert all(task.done() for task in service.workers)


def test_asyncio_is_available_for_the_suite():
    assert asyncio.get_event_loop_policy() is not None


# -- compression -----------------------------------------------------------
# The collector's otlphttp exporter enables gzip by default, so most real
# traffic arrives compressed. A receiver that ignores Content-Encoding 400s on
# every export from a default-configured collector.


def test_gzip_json_export_is_accepted(client):
    import gzip

    body = gzip.compress(json.dumps(otlp_json()).encode())
    response = client.post(
        "/v1/logs",
        content=body,
        headers={"content-type": "application/json", "content-encoding": "gzip"},
    )
    assert response.status_code == 200
    drain(client)
    assert client.app.state.service.pipeline.metrics.error_events == 1


def test_gzip_protobuf_export_is_accepted(client):
    import gzip

    response = client.post(
        "/v1/logs",
        content=gzip.compress(build_protobuf()),
        headers={"content-type": "application/x-protobuf", "content-encoding": "gzip"},
    )
    assert response.status_code == 200
    drain(client)
    assert client.app.state.service.pipeline.metrics.error_events == 1


def test_deflate_export_is_accepted(client):
    import zlib

    response = client.post(
        "/v1/logs",
        content=zlib.compress(json.dumps(otlp_json()).encode()),
        headers={"content-type": "application/json", "content-encoding": "deflate"},
    )
    assert response.status_code == 200


def test_identity_encoding_is_treated_as_uncompressed(client):
    response = client.post(
        "/v1/logs",
        content=json.dumps(otlp_json()),
        headers={"content-type": "application/json", "content-encoding": "identity"},
    )
    assert response.status_code == 200


def test_corrupt_gzip_returns_400_not_500(client):
    response = client.post(
        "/v1/logs",
        content=b"this is definitely not gzip",
        headers={"content-type": "application/json", "content-encoding": "gzip"},
    )
    assert response.status_code == 400
    assert "gzip" in response.json()["error"]


def test_unsupported_encoding_is_rejected(client):
    response = client.post(
        "/v1/logs",
        content=b"x",
        headers={"content-type": "application/json", "content-encoding": "br"},
    )
    assert response.status_code == 400


def test_decompression_bomb_is_refused():
    """A 64 MiB ceiling stops a tiny payload from expanding into memory pressure."""
    import gzip

    from err2issue.app import _MAX_DECOMPRESSED_BYTES, _decompress

    bomb = gzip.compress(b"\0" * (_MAX_DECOMPRESSED_BYTES + 1))
    with pytest.raises(ValueError, match="exceeds"):
        _decompress(bomb, "gzip")
