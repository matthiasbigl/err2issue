"""Core data model.

`ErrorEvent` is the single normalized representation of "an error happened".
Everything downstream — fingerprinting, redaction, routing, context assembly,
filing — operates on this type, never on raw OTLP.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

# OTel severity numbers: 17-20 == ERROR, 21-24 == FATAL.
# https://opentelemetry.io/docs/specs/otel/logs/data-model/#field-severitynumber
SEVERITY_ERROR = 17

_SEVERITY_NAMES = {
    1: "TRACE",
    5: "DEBUG",
    9: "INFO",
    13: "WARN",
    17: "ERROR",
    21: "FATAL",
}


def severity_name(number: int) -> str:
    """Map an OTel severity number to its range name (17..20 all -> ERROR)."""
    for base in sorted(_SEVERITY_NAMES, reverse=True):
        if number >= base:
            return _SEVERITY_NAMES[base]
    return "UNSPECIFIED"


@dataclass(frozen=True)
class ErrorEvent:
    """One error occurrence, decoded from an OTLP log record."""

    service_name: str
    exception_type: str
    exception_message: str = ""
    stacktrace: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    severity_number: int = SEVERITY_ERROR
    service_version: str | None = None
    body: str | None = None
    attributes: dict[str, str] = field(default_factory=dict)
    resource_attributes: dict[str, str] = field(default_factory=dict)

    @property
    def severity(self) -> str:
        return severity_name(self.severity_number)

    def with_redactions(self, redactor) -> ErrorEvent:
        """Return a copy with every free-text field passed through `redactor`."""
        return replace(
            self,
            exception_message=redactor(self.exception_message),
            stacktrace=redactor(self.stacktrace) if self.stacktrace else None,
            body=redactor(self.body) if self.body else None,
            attributes={k: redactor(v) for k, v in self.attributes.items()},
            resource_attributes={k: redactor(v) for k, v in self.resource_attributes.items()},
        )


@dataclass(frozen=True)
class LogLine:
    """A correlated (same trace) log record, used to build the context package."""

    timestamp: datetime
    severity: str
    text: str


@dataclass(frozen=True)
class FiledIssue:
    """Outcome of filing one error. Returned by every sink."""

    action: str  # "created" | "commented" | "reopened" | "skipped" | "dry-run"
    fingerprint: str
    repo: str
    number: int | None = None
    url: str | None = None
    count: int = 1
    detail: str = ""
