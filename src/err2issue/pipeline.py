"""The pipeline: OTLP in, filed issue out.

    decode -> select errors -> redact -> fingerprint -> suppress -> route
           -> enrich -> deliver

Ordering is deliberate. Redaction happens *before* fingerprinting so a secret
never influences an error's identity, and before enrichment so it is never sent
to a model. Suppression happens before routing and enrichment so a crash loop
costs one cheap dict lookup rather than an LLM call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import fingerprint as fp
from .ai import Enricher
from .context import TraceBuffer
from .models import ErrorEvent, FiledIssue, LogLine
from .redact import Redactor
from .routing import Router
from .sinks import Sink
from .suppress import Suppressor

log = logging.getLogger(__name__)


@dataclass
class Metrics:
    received_records: int = 0
    error_events: int = 0
    context_lines: int = 0
    suppressed: int = 0
    unrouted: int = 0
    filed_created: int = 0
    filed_commented: int = 0
    filed_reopened: int = 0
    filed_skipped: int = 0
    failed: int = 0
    suppression_reasons: dict[str, int] = field(default_factory=dict)

    def note_suppression(self, reason: str) -> None:
        self.suppressed += 1
        key = reason.split("(")[0].strip() or "suppressed"
        self.suppression_reasons[key] = self.suppression_reasons.get(key, 0) + 1

    def note_filed(self, result: FiledIssue) -> None:
        if result.action == "created":
            self.filed_created += 1
        elif result.action == "commented":
            self.filed_commented += 1
        elif result.action == "reopened":
            self.filed_reopened += 1
        elif result.action == "skipped":
            self.filed_skipped += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "received_records": self.received_records,
            "error_events": self.error_events,
            "context_lines": self.context_lines,
            "suppressed": self.suppressed,
            "unrouted": self.unrouted,
            "filed": {
                "created": self.filed_created,
                "commented": self.filed_commented,
                "reopened": self.filed_reopened,
                "skipped": self.filed_skipped,
            },
            "failed": self.failed,
            "suppression_reasons": dict(self.suppression_reasons),
        }

    def as_prometheus(self) -> str:
        lines = [
            "# HELP err2issue_records_received_total OTLP log records received.",
            "# TYPE err2issue_records_received_total counter",
            f"err2issue_records_received_total {self.received_records}",
            "# HELP err2issue_error_events_total Records selected as errors.",
            "# TYPE err2issue_error_events_total counter",
            f"err2issue_error_events_total {self.error_events}",
            "# HELP err2issue_suppressed_total Errors dropped by storm suppression.",
            "# TYPE err2issue_suppressed_total counter",
            f"err2issue_suppressed_total {self.suppressed}",
            "# HELP err2issue_unrouted_total Errors with no destination repository.",
            "# TYPE err2issue_unrouted_total counter",
            f"err2issue_unrouted_total {self.unrouted}",
            "# HELP err2issue_filed_total Filing outcomes by action.",
            "# TYPE err2issue_filed_total counter",
            f'err2issue_filed_total{{action="created"}} {self.filed_created}',
            f'err2issue_filed_total{{action="commented"}} {self.filed_commented}',
            f'err2issue_filed_total{{action="reopened"}} {self.filed_reopened}',
            f'err2issue_filed_total{{action="skipped"}} {self.filed_skipped}',
            "# HELP err2issue_failed_total Errors that could not be filed.",
            "# TYPE err2issue_failed_total counter",
            f"err2issue_failed_total {self.failed}",
        ]
        return "\n".join(lines) + "\n"


class Pipeline:
    def __init__(
        self,
        sink: Sink,
        router: Router,
        suppressor: Suppressor,
        redactor: Redactor,
        enricher: Enricher,
        trace_buffer: TraceBuffer | None = None,
        max_context_log_lines: int = 20,
        drop_unrouted: bool = True,
    ):
        self.sink = sink
        self.router = router
        self.suppressor = suppressor
        self.redactor = redactor
        self.enricher = enricher
        self.traces = trace_buffer or TraceBuffer()
        self.max_context_log_lines = max_context_log_lines
        self.drop_unrouted = drop_unrouted
        self.metrics = Metrics()

    def absorb_context(self, lines: list[LogLine], trace_ids: list[str | None]) -> None:
        for line, trace_id in zip(lines, trace_ids, strict=False):
            self.traces.add(trace_id, line)
            self.metrics.context_lines += 1

    async def handle(self, event: ErrorEvent) -> FiledIssue | None:
        """Run one error event end to end. Returns None when it was dropped."""
        self.metrics.error_events += 1

        # 1. Redact first — before identity, before the model, before GitHub.
        event = event.with_redactions(self.redactor)

        # 2. Identity.
        fingerprint = fp.compute(event)

        # 3. Throttle. Cheapest possible check, so it comes before any I/O.
        decision = self.suppressor.check(fingerprint)
        if not decision:
            self.metrics.note_suppression(decision.reason)
            log.debug("suppressed %s for %s: %s", fingerprint, event.service_name, decision.reason)
            return None

        # 4. Destination.
        repo = self.router.resolve(event.service_name)
        if not repo:
            self.metrics.unrouted += 1
            message = (
                f"no repository for service {event.service_name!r}; "
                "set E2I_GITHUB_REPO or add a rule to E2I_ROUTE_MAP"
            )
            if self.drop_unrouted:
                log.warning("%s (dropping)", message)
                return None
            raise ValueError(message)

        # 5. Title and summary. Never fatal — falls back deterministically.
        enrichment = await self.enricher.enrich(event)

        # 6. Correlated context.
        correlated = self.traces.get(event.trace_id, limit=self.max_context_log_lines)

        # 7. Deliver.
        try:
            result = await self.sink.deliver(
                event=event,
                fingerprint=fingerprint,
                repo=repo,
                summary=enrichment.title,
                correlated=correlated,
            )
        except Exception:
            self.metrics.failed += 1
            log.exception("failed to file %s for %s in %s", fingerprint, event.service_name, repo)
            return None

        self.metrics.note_filed(result)
        log.info(
            "%s %s#%s for %s (%s) [%s]",
            result.action,
            result.repo,
            result.number if result.number is not None else "?",
            event.service_name,
            fingerprint,
            enrichment.source,
        )
        return result

    async def aclose(self) -> None:
        await self.sink.aclose()
