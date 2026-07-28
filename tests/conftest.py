"""Shared fixtures and builders.

Two principles here:
* Tests never touch the network. `respx` intercepts httpx; there is no live
  GitHub and no live model in the suite.
* Time is injected, never slept on. Every rate limit and window is tested by
  advancing a fake clock, so the suite stays fast and deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from err2issue.models import ErrorEvent, LogLine

PY_TRACE = """Traceback (most recent call last):
  File "/build/9f2a1c/src/app/handlers.py", line 142, in handle_checkout
    total = cart.total()
  File "/build/9f2a1c/src/app/cart.py", line 88, in total
    return sum(i.price for i in self.items)
TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'
"""

JAVA_TRACE = """java.lang.NullPointerException: Cannot invoke "User.getId()"
\tat com.acme.profile.ProfileService.load(ProfileService.java:57)
\tat com.acme.profile.ProfileController.get(ProfileController.java:31)
\t... 42 more
"""

NODE_TRACE = """TypeError: Cannot read properties of undefined (reading 'id')
    at getUser (/srv/app/dist/user.js:22:17)
    at process (/srv/app/dist/index.js:104:9)
"""


class FakeClock:
    """Monotonic-style clock the tests advance by hand."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_event(**overrides) -> ErrorEvent:
    defaults = dict(
        service_name="checkout-api",
        service_version="1.4.2",
        exception_type="TypeError",
        exception_message="unsupported operand type(s) for +: 'int' and 'NoneType'",
        stacktrace=PY_TRACE,
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        span_id="00f067aa0ba902b7",
        timestamp=datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC),
        severity_number=17,
        attributes={"http.route": "/checkout", "http.status_code": "500"},
        resource_attributes={"service.name": "checkout-api", "service.version": "1.4.2"},
    )
    defaults.update(overrides)
    return ErrorEvent(**defaults)


def make_log_line(text: str = "handling request", severity: str = "INFO") -> LogLine:
    return LogLine(
        timestamp=datetime(2026, 7, 28, 11, 59, 59, tzinfo=UTC), severity=severity, text=text
    )


def otlp_json(
    service: str = "checkout-api",
    severity_number: int = 17,
    exception_type: str | None = "TypeError",
    message: str = "boom",
    stacktrace: str | None = PY_TRACE,
    trace_id: str | None = "4bf92f3577b34da6a3ce929d0e0e4736",
    body: str = "",
) -> dict:
    attributes = []
    if exception_type:
        attributes.append({"key": "exception.type", "value": {"stringValue": exception_type}})
        attributes.append({"key": "exception.message", "value": {"stringValue": message}})
        if stacktrace:
            attributes.append({"key": "exception.stacktrace", "value": {"stringValue": stacktrace}})
    record: dict = {
        "timeUnixNano": "1785585600000000000",
        "severityNumber": severity_number,
        "severityText": "ERROR" if severity_number >= 17 else "INFO",
        "attributes": attributes,
    }
    if body:
        record["body"] = {"stringValue": body}
    if trace_id:
        record["traceId"] = trace_id
    return {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service}},
                        {"key": "service.version", "value": {"stringValue": "1.4.2"}},
                    ]
                },
                "scopeLogs": [{"logRecords": [record]}],
            }
        ]
    }


def issue_payload(
    number: int = 7,
    title: str = "[x1] TypeError in checkout",
    state: str = "open",
    body: str | None = None,
    fingerprint: str = "abc123def456",
    count: int = 1,
) -> dict:
    if body is None:
        body = f"<!-- err2issue: fingerprint={fingerprint} version=v1 count={count} -->\nbody"
    return {
        "number": number,
        "title": title,
        "state": state,
        "body": body,
        "html_url": f"https://github.com/acme/api/issues/{number}",
        "updated_at": "2026-07-28T12:00:00Z",
    }


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def api_url() -> str:
    return "https://api.github.com"


async def no_sleep(_seconds: float) -> None:
    """Drop-in for asyncio.sleep so retry/backoff paths run instantly."""
    return None
