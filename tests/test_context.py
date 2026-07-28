"""The issue body is the coupling seam (PLAN.md §6), so it is a contract test.

Anything a consumer is told it may parse — the machine header, the `[xN]` title
convention — is asserted here.
"""

from __future__ import annotations

from datetime import UTC, datetime

from err2issue import context as ctx
from tests.conftest import PY_TRACE, make_event, make_log_line

# -- machine-readable header -----------------------------------------------


def test_header_round_trips():
    header = ctx.machine_header("abc123def456", "v1", 12)
    assert ctx.parse_header(header) == {"fingerprint": "abc123def456", "version": "v1", "count": 12}


def test_header_survives_surrounding_prose():
    body = f"intro\n\n{ctx.machine_header('aaaabbbbcccc', 'v1', 3)}\n\nrest of body"
    assert ctx.parse_header(body)["count"] == 3


def test_parse_header_returns_none_for_a_hand_written_issue():
    assert ctx.parse_header("just a normal issue someone filed") is None
    assert ctx.parse_header(None) is None
    assert ctx.parse_header("") is None


def test_built_body_contains_a_parseable_header():
    body = ctx.build_body(make_event(), "abc123def456", "v1", "summary", count=5)
    assert ctx.parse_header(body) == {"fingerprint": "abc123def456", "version": "v1", "count": 5}


# -- title convention ------------------------------------------------------


def test_title_count_round_trips():
    title = ctx.format_title(12, "TypeError in checkout")
    assert title == "[x12] TypeError in checkout"
    assert ctx.parse_title_count(title) == (12, "TypeError in checkout")


def test_untagged_title_reads_as_count_one():
    assert ctx.parse_title_count("Plain title") == (1, "Plain title")


def test_title_is_truncated_to_stay_readable():
    title = ctx.format_title(1, "x" * 200)
    _, stem = ctx.parse_title_count(title)
    assert len(stem) <= 70


def test_title_collapses_whitespace():
    assert ctx.format_title(1, "a\n\n  b   c") == "[x1] a b c"


def test_empty_summary_still_produces_a_usable_title():
    assert ctx.parse_title_count(ctx.format_title(1, ""))[1] == "Unknown error"


def test_recounting_a_title_does_not_nest_prefixes():
    """Bumping [x1] to [x2] must not produce '[x2] [x1] ...'."""
    count, stem = ctx.parse_title_count("[x1] TypeError in checkout")
    assert ctx.format_title(count + 1, stem) == "[x2] TypeError in checkout"


# -- body content ----------------------------------------------------------


def test_body_carries_everything_an_agent_needs_without_the_backend():
    event = make_event()
    body = ctx.build_body(
        event,
        "abc123def456",
        "v1",
        "Cart total fails on a missing price",
        correlated=[make_log_line("GET /checkout")],
    )
    for expected in (
        "checkout-api",  # service
        "1.4.2",  # version
        "TypeError",  # exception type
        "cart.py",  # stack
        event.trace_id,  # trace correlation
        "GET /checkout",  # correlated logs
        "http.route",  # runtime attributes
        "Cart total fails",  # summary
    ):
        assert expected in body, f"missing {expected!r}"


def test_body_omits_the_summary_section_when_there_is_none():
    body = ctx.build_body(make_event(), "abc", "v1", "")
    assert "### Summary" not in body


def test_body_omits_stack_section_when_there_is_no_stack():
    body = ctx.build_body(make_event(stacktrace=None), "abc", "v1", "s")
    assert "### Stack trace" not in body


def test_body_omits_correlated_section_when_there_are_no_lines():
    body = ctx.build_body(make_event(), "abc", "v1", "s", correlated=[])
    assert "Correlated log lines" not in body


def test_exception_attributes_are_not_repeated_in_the_attribute_table():
    event = make_event(attributes={"exception.type": "TypeError", "http.route": "/x"})
    body = ctx.build_body(event, "abc", "v1", "s")
    table = body.split("Runtime attributes")[-1]
    assert "http.route" in table
    assert "`exception.type`" not in table


def test_long_stacktrace_is_truncated_with_a_marker():
    event = make_event(stacktrace="line\n" * 5000)
    body = ctx.build_body(event, "abc", "v1", "s", max_stacktrace_chars=500)
    assert "truncated" in body
    assert len(body) < 5000


def test_truncate_leaves_short_text_alone():
    assert ctx.truncate("short", 100) == "short"
    assert ctx.truncate("", 10) == ""
    assert ctx.truncate(None, 10) == ""


# -- occurrence comments ---------------------------------------------------


def test_occurrence_comment_states_the_count_and_time():
    comment = ctx.build_occurrence_comment(make_event(), count=7)
    assert "Occurrence #7" in comment
    assert "2026-07-28" in comment


def test_regression_comment_is_marked_and_carries_the_stack():
    comment = ctx.build_occurrence_comment(make_event(), count=3, regression=True)
    assert "Regression" in comment
    assert "Reopening" in comment
    assert "cart.py" in comment


def test_routine_occurrence_comment_omits_the_stack_to_stay_short():
    comment = ctx.build_occurrence_comment(make_event(), count=3, regression=False)
    assert "Traceback" not in comment


# -- deterministic fallback title ------------------------------------------


def test_fallback_summary_uses_type_and_message():
    summary = ctx.fallback_summary(make_event(exception_message="cannot add int and None"))
    assert summary.startswith("TypeError")
    assert "cannot add int and None" in summary


def test_fallback_summary_without_a_message_uses_the_service():
    summary = ctx.fallback_summary(make_event(exception_message=""))
    assert "TypeError" in summary
    assert "checkout-api" in summary


def test_fallback_summary_respects_the_length_budget():
    summary = ctx.fallback_summary(make_event(exception_message="y" * 500), max_chars=70)
    assert len(summary) <= 70


# -- trace buffer ----------------------------------------------------------


def test_trace_buffer_returns_lines_for_the_matching_trace_only():
    buffer = ctx.TraceBuffer()
    buffer.add("trace-a", make_log_line("a1"))
    buffer.add("trace-b", make_log_line("b1"))
    assert [line.text for line in buffer.get("trace-a")] == ["a1"]


def test_trace_buffer_ignores_records_without_a_trace_id():
    buffer = ctx.TraceBuffer()
    buffer.add(None, make_log_line("orphan"))
    assert len(buffer) == 0
    assert buffer.get(None) == []


def test_trace_buffer_returns_the_most_recent_lines():
    buffer = ctx.TraceBuffer()
    for i in range(50):
        buffer.add("t", make_log_line(f"line{i}"))
    recent = buffer.get("t", limit=5)
    assert [line.text for line in recent] == [f"line{i}" for i in range(45, 50)]


def test_trace_buffer_evicts_oldest_traces_when_full():
    buffer = ctx.TraceBuffer(max_traces=3)
    for i in range(10):
        buffer.add(f"trace{i}", make_log_line("x"))
    assert len(buffer) == 3
    assert buffer.get("trace0") == []
    assert buffer.get("trace9") != []


def test_trace_buffer_caps_lines_per_trace():
    buffer = ctx.TraceBuffer(max_lines_per_trace=4)
    for i in range(20):
        buffer.add("t", make_log_line(f"l{i}"))
    assert len(buffer.get("t", limit=100)) == 4


def test_unknown_trace_returns_no_lines():
    assert ctx.TraceBuffer().get("never-seen") == []


def test_body_renders_first_seen_distinct_from_last_seen():
    event = make_event(timestamp=datetime(2026, 7, 28, 12, 0, tzinfo=UTC))
    body = ctx.build_body(
        event,
        "abc",
        "v1",
        "s",
        count=4,
        first_seen=datetime(2026, 7, 1, 9, 0, tzinfo=UTC),
    )
    assert "2026-07-01 09:00:00 UTC" in body
    assert "2026-07-28 12:00:00 UTC" in body


def test_body_is_valid_markdown_without_stray_code_fences():
    body = ctx.build_body(make_event(stacktrace=PY_TRACE), "abc", "v1", "s")
    assert body.count("```") % 2 == 0, "unbalanced code fences would break rendering"
