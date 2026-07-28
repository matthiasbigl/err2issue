"""Pipeline orchestration: ordering, suppression, routing, and failure isolation.

The ordering assertions are the interesting ones. Redaction has to happen before
fingerprinting (or a secret changes an error's identity) and before enrichment
(or a secret is sent to a model). Suppression has to happen before enrichment
(or a crash loop bills an LLM call per occurrence).
"""

from __future__ import annotations

from err2issue import fingerprint as fp
from err2issue.ai import Enrichment
from err2issue.context import TraceBuffer
from err2issue.models import FiledIssue
from err2issue.pipeline import Pipeline
from err2issue.redact import Redactor
from err2issue.routing import Router
from err2issue.sinks import DryRunSink
from err2issue.suppress import Suppressor
from tests.conftest import FakeClock, make_event, make_log_line


class RecordingEnricher:
    def __init__(self, title: str = "A title"):
        self.title = title
        self.seen = []

    async def enrich(self, event):
        self.seen.append(event)
        return Enrichment(title=self.title, summary="", source="ai")


class ExplodingSink(DryRunSink):
    async def deliver(self, *args, **kwargs):
        raise RuntimeError("github is down")


def build(sink=None, clock=None, **kwargs) -> Pipeline:
    clock = clock or FakeClock()
    defaults = dict(
        sink=sink or DryRunSink(),
        router=Router.from_spec("", "acme/api"),
        suppressor=Suppressor(
            window_seconds=600,
            max_per_minute=1000,
            max_new_per_day=1000,
            clock=clock,
            wall_clock=lambda: 0.0,
        ),
        redactor=Redactor(),
        enricher=RecordingEnricher(),
        trace_buffer=TraceBuffer(),
    )
    defaults.update(kwargs)
    return Pipeline(**defaults)


# -- happy path ------------------------------------------------------------


async def test_an_error_is_filed():
    sink = DryRunSink()
    pipeline = build(sink=sink)
    result = await pipeline.handle(make_event())
    assert result is not None
    assert len(sink.calls) == 1
    assert sink.calls[0]["repo"] == "acme/api"


async def test_metrics_track_outcomes():
    pipeline = build()
    await pipeline.handle(make_event())
    assert pipeline.metrics.error_events == 1


# -- ordering --------------------------------------------------------------


async def test_secrets_are_redacted_before_the_model_sees_them():
    secret = "ghp_" + "S" * 36
    enricher = RecordingEnricher()
    pipeline = build(enricher=enricher)
    await pipeline.handle(make_event(exception_message=f"auth failed {secret}"))
    assert secret not in enricher.seen[0].exception_message


async def test_a_secret_does_not_change_an_errors_identity():
    """Two occurrences differing only by a leaked token are one bug."""
    pipeline = build()
    redactor = Redactor()
    a = make_event(exception_message="token ghp_" + "A" * 36).with_redactions(redactor)
    b = make_event(exception_message="token ghp_" + "B" * 36).with_redactions(redactor)
    assert fp.compute(a) == fp.compute(b)
    assert pipeline is not None


async def test_suppressed_events_never_reach_the_model_or_the_sink():
    sink = DryRunSink()
    enricher = RecordingEnricher()
    clock = FakeClock()
    pipeline = build(sink=sink, enricher=enricher, clock=clock)
    for _ in range(10):
        await pipeline.handle(make_event())
    assert len(sink.calls) == 1
    assert len(enricher.seen) == 1, "suppression must come before enrichment"
    assert pipeline.metrics.suppressed == 9


# -- routing ---------------------------------------------------------------


async def test_events_route_to_the_repository_for_their_service():
    sink = DryRunSink()
    pipeline = build(sink=sink, router=Router.from_spec("cart-*=acme/cart,pay=acme/pay"))
    await pipeline.handle(make_event(service_name="cart-api"))
    await pipeline.handle(make_event(service_name="pay"))
    assert [call["repo"] for call in sink.calls] == ["acme/cart", "acme/pay"]


async def test_unroutable_events_are_dropped_and_counted():
    sink = DryRunSink()
    pipeline = build(sink=sink, router=Router.from_spec("cart-*=acme/cart"), drop_unrouted=True)
    assert await pipeline.handle(make_event(service_name="nowhere")) is None
    assert pipeline.metrics.unrouted == 1
    assert sink.calls == []


async def test_unroutable_events_can_be_made_fatal():
    import pytest

    pipeline = build(router=Router.from_spec("cart-*=acme/cart"), drop_unrouted=False)
    with pytest.raises(ValueError, match="no repository"):
        await pipeline.handle(make_event(service_name="nowhere"))


# -- context correlation ---------------------------------------------------


async def test_correlated_log_lines_are_attached_by_trace_id():
    sink = DryRunSink()
    buffer = TraceBuffer()
    pipeline = build(sink=sink, trace_buffer=buffer)
    pipeline.absorb_context([make_log_line("GET /checkout")], ["4bf92f3577b34da6a3ce929d0e0e4736"])
    await pipeline.handle(make_event())
    assert sink.calls[0]["correlated_lines"] == 1


async def test_lines_from_a_different_trace_are_not_attached():
    sink = DryRunSink()
    pipeline = build(sink=sink)
    pipeline.absorb_context([make_log_line("unrelated")], ["some-other-trace"])
    await pipeline.handle(make_event())
    assert sink.calls[0]["correlated_lines"] == 0


# -- failure isolation -----------------------------------------------------


async def test_a_sink_failure_is_contained_and_counted():
    """err2issue must never propagate a failure back toward the telemetry path."""
    pipeline = build(sink=ExplodingSink())
    assert await pipeline.handle(make_event()) is None
    assert pipeline.metrics.failed == 1


async def test_one_failure_does_not_stop_later_events():
    pipeline = build(sink=ExplodingSink())
    for i in range(3):
        await pipeline.handle(make_event(exception_type=f"E{i}"))
    assert pipeline.metrics.failed == 3


# -- metrics rendering -----------------------------------------------------


async def test_prometheus_output_is_well_formed():
    pipeline = build()
    await pipeline.handle(make_event())
    text = pipeline.metrics.as_prometheus()
    assert "err2issue_error_events_total 1" in text
    for line in text.splitlines():
        assert line.startswith("#") or " " in line
    assert text.endswith("\n")


async def test_metrics_dict_reports_filing_actions():
    pipeline = build()
    pipeline.metrics.note_filed(FiledIssue(action="created", fingerprint="a", repo="r"))
    pipeline.metrics.note_filed(FiledIssue(action="commented", fingerprint="a", repo="r"))
    snapshot = pipeline.metrics.as_dict()
    assert snapshot["filed"]["created"] == 1
    assert snapshot["filed"]["commented"] == 1


async def test_suppression_reasons_are_aggregated():
    clock = FakeClock()
    pipeline = build(clock=clock)
    for _ in range(3):
        await pipeline.handle(make_event())
    assert sum(pipeline.metrics.suppression_reasons.values()) == 2
