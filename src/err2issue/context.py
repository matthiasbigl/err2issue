"""The context package — what a human or a fix agent reads instead of the backend.

PLAN.md §6 makes the issue format the coupling seam, so it is a contract, not a
presentation detail. The machine-readable header is what consumers parse; the
rest is what people read. See docs/ISSUE_CONTRACT.md.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict, deque
from datetime import datetime

from .models import ErrorEvent, LogLine

HEADER_RE = re.compile(
    r"<!--\s*err2issue:\s*fingerprint=(?P<fp>[0-9a-f]+)\s+"
    r"version=(?P<ver>v\d+)\s+count=(?P<count>\d+)\s*-->"
)
TITLE_COUNT_RE = re.compile(r"^\[x(?P<count>\d+)\]\s*(?P<rest>.*)$")

# Attributes that are noise in an issue body — they are either already shown
# elsewhere or are per-occurrence values that would make every issue look unique.
_SKIPPED_ATTRS = {
    "exception.type",
    "exception.message",
    "exception.stacktrace",
    "exception.escaped",
}


class TraceBuffer:
    """Bounded ring buffer of recent log lines, keyed by trace id.

    Lets the issue show the last few log lines that shared the failing request's
    trace, which is usually the difference between an actionable report and a
    bare stack trace.
    """

    def __init__(self, max_traces: int = 500, max_lines_per_trace: int = 50):
        self.max_traces = max_traces
        self.max_lines_per_trace = max_lines_per_trace
        self._traces: OrderedDict[str, deque[LogLine]] = OrderedDict()
        self._lock = threading.Lock()

    def add(self, trace_id: str | None, line: LogLine) -> None:
        if not trace_id:
            return
        with self._lock:
            bucket = self._traces.get(trace_id)
            if bucket is None:
                bucket = deque(maxlen=self.max_lines_per_trace)
                self._traces[trace_id] = bucket
            bucket.append(line)
            self._traces.move_to_end(trace_id)
            while len(self._traces) > self.max_traces:
                self._traces.popitem(last=False)

    def get(self, trace_id: str | None, limit: int = 20) -> list[LogLine]:
        if not trace_id:
            return []
        with self._lock:
            bucket = self._traces.get(trace_id)
            return list(bucket)[-limit:] if bucket else []

    def __len__(self) -> int:
        with self._lock:
            return len(self._traces)


def truncate(text: str | None, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated, {len(text) - limit} more characters]"


def _fmt_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S UTC")


def machine_header(fingerprint: str, version: str, count: int) -> str:
    return f"<!-- err2issue: fingerprint={fingerprint} version={version} count={count} -->"


def parse_header(body: str | None) -> dict[str, str | int] | None:
    """Read the machine-readable header back out of an issue body."""
    if not body:
        return None
    match = HEADER_RE.search(body)
    if not match:
        return None
    return {
        "fingerprint": match.group("fp"),
        "version": match.group("ver"),
        "count": int(match.group("count")),
    }


def parse_title_count(title: str) -> tuple[int, str]:
    """Split `[x12] Something broke` into (12, 'Something broke')."""
    match = TITLE_COUNT_RE.match(title or "")
    if not match:
        return 1, (title or "").strip()
    return int(match.group("count")), match.group("rest").strip()


def format_title(count: int, summary: str, max_chars: int = 70) -> str:
    summary = " ".join((summary or "Unknown error").split())
    if len(summary) > max_chars:
        summary = summary[: max_chars - 1].rstrip() + "…"
    return f"[x{count}] {summary}"


def build_body(
    event: ErrorEvent,
    fingerprint: str,
    version: str,
    summary: str,
    count: int = 1,
    first_seen: datetime | None = None,
    correlated: list[LogLine] | None = None,
    max_message_chars: int = 2000,
    max_stacktrace_chars: int = 6000,
    max_log_lines: int = 20,
) -> str:
    first = first_seen or event.timestamp
    parts: list[str] = [machine_header(fingerprint, version, count), ""]

    parts.append(f"**`{event.exception_type}`** in **{event.service_name}**")
    parts.append("")

    rows = [
        ("First seen", _fmt_time(first)),
        ("Last seen", _fmt_time(event.timestamp)),
        ("Occurrences", str(count)),
        ("Service", f"`{event.service_name}`"),
        ("Version", f"`{event.service_version}`" if event.service_version else "_unknown_"),
        ("Severity", f"`{event.severity}`"),
        ("Fingerprint", f"`{version}:{fingerprint}`"),
    ]
    if event.trace_id:
        rows.append(("Trace ID", f"`{event.trace_id}`"))
    if event.span_id:
        rows.append(("Span ID", f"`{event.span_id}`"))

    parts.append("| | |")
    parts.append("|---|---|")
    parts.extend(f"| {name} | {value} |" for name, value in rows)
    parts.append("")

    if summary:
        parts.append("### Summary")
        parts.append("")
        parts.append(summary)
        parts.append("")

    parts.append("### Exception")
    parts.append("")
    parts.append("```")
    parts.append(f"{event.exception_type}: {truncate(event.exception_message, max_message_chars)}")
    parts.append("```")
    parts.append("")

    if event.stacktrace:
        parts.append("### Stack trace")
        parts.append("")
        parts.append("```")
        parts.append(truncate(event.stacktrace, max_stacktrace_chars))
        parts.append("```")
        parts.append("")

    lines = (correlated or [])[-max_log_lines:]
    if lines:
        parts.append(f"### Correlated log lines (trace `{event.trace_id}`)")
        parts.append("")
        parts.append("```")
        for line in lines:
            parts.append(f"{_fmt_time(line.timestamp)}  {line.severity:<5}  {line.text}")
        parts.append("```")
        parts.append("")

    attributes = {
        key: value
        for key, value in sorted({**event.resource_attributes, **event.attributes}.items())
        if key not in _SKIPPED_ATTRS and value
    }
    if attributes:
        parts.append("<details><summary>Runtime attributes</summary>")
        parts.append("")
        parts.append("| Attribute | Value |")
        parts.append("|---|---|")
        for key, value in attributes.items():
            parts.append(f"| `{key}` | `{truncate(value, 200)}` |")
        parts.append("")
        parts.append("</details>")
        parts.append("")

    parts.append("---")
    parts.append(
        "<sub>Filed automatically by "
        "[err2issue](https://github.com/matthiasbigl/err2issue). "
        "Occurrence count is in the title; this body always reflects the most "
        "recent occurrence.</sub>"
    )
    return "\n".join(parts)


def build_occurrence_comment(
    event: ErrorEvent,
    count: int,
    correlated: list[LogLine] | None = None,
    regression: bool = False,
    max_log_lines: int = 10,
    max_stacktrace_chars: int = 2000,
) -> str:
    parts: list[str] = []
    if regression:
        parts.append("### Regression")
        parts.append("")
        parts.append("This error returned after the issue was closed. Reopening.")
    else:
        parts.append(f"### Occurrence #{count}")
    parts.append("")
    parts.append(f"- **Seen at** {_fmt_time(event.timestamp)}")
    if event.service_version:
        parts.append(f"- **Version** `{event.service_version}`")
    if event.trace_id:
        parts.append(f"- **Trace** `{event.trace_id}`")
    parts.append("")

    if event.exception_message:
        parts.append("```")
        parts.append(f"{event.exception_type}: {truncate(event.exception_message, 500)}")
        parts.append("```")
        parts.append("")

    lines = (correlated or [])[-max_log_lines:]
    if lines:
        parts.append("<details><summary>Correlated log lines</summary>")
        parts.append("")
        parts.append("```")
        for line in lines:
            parts.append(f"{_fmt_time(line.timestamp)}  {line.severity:<5}  {line.text}")
        parts.append("```")
        parts.append("")
        parts.append("</details>")
        parts.append("")

    if regression and event.stacktrace:
        parts.append("<details><summary>Stack trace</summary>")
        parts.append("")
        parts.append("```")
        parts.append(truncate(event.stacktrace, max_stacktrace_chars))
        parts.append("```")
        parts.append("")
        parts.append("</details>")
    return "\n".join(parts).rstrip()


def fallback_summary(event: ErrorEvent, max_chars: int = 70) -> str:
    """Deterministic title used when AI is unconfigured or unreachable.

    PLAN.md §5.3: the AI step is an enhancement, never a dependency.
    """
    message = " ".join((event.exception_message or "").split())
    first_line = message.split(". ")[0] if message else ""
    if first_line:
        return f"{event.exception_type}: {first_line}"[:max_chars].rstrip()
    return f"{event.exception_type} in {event.service_name}"[:max_chars].rstrip()
