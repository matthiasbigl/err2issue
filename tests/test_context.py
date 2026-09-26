"""The issue body is the coupling seam (PLAN.md §6), so it is a contract test.

Anything a consumer is told it may parse — the machine header, the `[xN]` title
convention — is asserted here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

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
    assert "characters omitted" in body
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


# -- the log record body (a bare `str(exc)` must not hide the log line) -----

SHIELDED = dict(
    exception_type="ConnectionClosedError",
    exception_message="None",
    stacktrace=None,
    body="ConnectionClosedError exception in shielded future",
)


def test_body_shows_the_log_message_next_to_the_exception():
    body = ctx.build_body(make_event(**SHIELDED), "abc", "v2", "")
    assert "### Log message" in body
    assert "ConnectionClosedError exception in shielded future" in body
    assert body.index("### Log message") < body.index("### Exception")


def test_body_omits_a_log_message_that_repeats_the_exception():
    event = make_event(exception_message="db down", body="db down")
    assert "### Log message" not in ctx.build_body(event, "abc", "v2", "")


def test_body_omits_the_log_message_when_there_is_none():
    assert "### Log message" not in ctx.build_body(make_event(body=None), "abc", "v2", "")


def test_empty_exception_message_renders_without_a_dangling_colon():
    body = ctx.build_body(make_event(exception_message="", body="x"), "abc", "v2", "")
    assert "```\nTypeError\n```" in body


def test_occurrence_comment_carries_the_log_message():
    comment = ctx.build_occurrence_comment(make_event(**SHIELDED), count=2)
    assert "```\nConnectionClosedError exception in shielded future\n```" in comment


@pytest.mark.parametrize("placeholder", ["None", "null", "", "  ", "undefined", "TypeError"])
def test_placeholder_messages_are_uninformative(placeholder):
    assert ctx.is_uninformative(placeholder, "TypeError")


def test_a_real_message_is_informative():
    assert not ctx.is_uninformative("None of the replicas answered", "TypeError")


def test_fallback_title_uses_the_log_message_when_the_exception_says_nothing():
    summary = ctx.fallback_summary(make_event(**SHIELDED))
    assert summary == "ConnectionClosedError exception in shielded future"


def test_fallback_title_prefixes_the_type_when_the_log_line_omits_it():
    event = make_event(**{**SHIELDED, "body": "lost upstream during checkout"})
    assert ctx.fallback_summary(event) == "ConnectionClosedError: lost upstream during checkout"


def test_fallback_title_prefers_an_informative_exception_message():
    event = make_event(exception_message="cart is empty", body="checkout failed")
    assert ctx.fallback_summary(event) == "TypeError: cart is empty"


def test_fence_survives_backticks_in_the_content():
    fenced = ctx.fence("before\n```\ninjected\n```\nafter")
    assert fenced.startswith("````\n") and fenced.endswith("\n````")


# -- Markdown safety: exception text is attacker-controlled ------------------


def _stack_section(body: str) -> str:
    return body.split("### Stack trace\n\n", 1)[1].split("\n\n", 1)[0]


def test_a_stack_containing_a_fence_stays_inside_one_block():
    stack = "frame one\n```\n## injected heading\n```\nframe two"
    body = ctx.build_body(make_event(stacktrace=stack), "abc", "v2", "")
    section = _stack_section(body)
    assert section.startswith("````\n") and "\n````" in section
    assert "## injected heading" in section
    # nothing leaks out as real Markdown between the stack and the next heading
    assert "\n## injected heading" not in body.replace(section, "")


def test_correlated_lines_and_comment_blocks_use_a_safe_fence():
    line = make_log_line("evil ``` line")
    body = ctx.build_body(make_event(), "abc", "v2", "", correlated=[line])
    assert "````\n2026-07-28 11:59:59 UTC  INFO   evil ``` line\n````" in body
    comment = ctx.build_occurrence_comment(
        make_event(stacktrace="a\n```\nb"), count=2, correlated=[line], regression=True
    )
    assert comment.count("````") == 4


def _table_rows(body: str) -> list[str]:
    return [line for line in body.splitlines() if line.startswith("| ")]


def test_an_attribute_value_with_a_pipe_and_newline_stays_on_one_row():
    event = make_event(attributes={"custom.note": "a|b\nc"})
    body = ctx.build_body(event, "abc", "v2", "")
    rows = [row for row in _table_rows(body) if "custom.note" in row]
    assert rows == ["| `custom.note` | `a\\|b c` |"]


def test_cell_escapes_backslashes_that_precede_a_pipe():
    # `\\|` would read as an escaped backslash followed by a live cell separator.
    assert ctx._cell("a\\|b") == "`a\\\\\\|b`"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("plain", "`plain`"),
        ("has `tick` inside", "``has `tick` inside``"),
        ("ends `tick`", "`` ends `tick` ``"),
        ("`starts", "`` `starts ``"),
        ("run ``` of three", "````run ``` of three````"),
        ("", ""),
    ],
)
def test_code_span_outlasts_the_backticks_inside_it(value, expected):
    assert ctx._code(value) == expected


def test_service_and_version_rows_are_escaped():
    event = make_event(service_name="svc|x", service_version="1`2")
    rows = _table_rows(ctx.build_body(event, "abc", "v2", ""))
    assert "| Service | `svc\\|x` |" in rows
    assert "| Version | ``1`2`` |" in rows


def test_headline_escapes_markdown_in_the_service_name():
    event = make_event(service_name="**bold** [x](y)\n# h", exception_type="Err`or")
    headline = ctx.build_body(event, "abc", "v2", "").splitlines()[2]
    assert headline == "**``Err`or``** in **\\*\\*bold\\*\\* \\[x\\](y) \\# h**"


def test_attribute_table_is_capped_with_a_more_row():
    attrs = {f"k{i:03}": "v" for i in range(60)}
    body = ctx.build_body(make_event(attributes=attrs), "abc", "v2", "")
    table = body.split("Runtime attributes")[1]
    # 60 custom + 2 resource attributes from make_event, minus 50 shown
    assert "| _+12 more_ | |" in table
    assert "`k049`" in table and "`k050`" not in table


def test_a_long_correlated_line_is_capped_on_one_line():
    body = ctx.build_body(make_event(), "abc", "v2", "", correlated=[make_log_line("x" * 2000)])
    block = body.split("### Correlated log lines")[1]
    assert "x" * 500 + " … [1500 more characters]" in block
    assert "x" * 501 not in block


# -- head-and-tail truncation ------------------------------------------------


def test_truncate_middle_keeps_the_final_python_frame_and_the_exception():
    frames = "".join(
        f'  File "/app/src/mod{i}.py", line {i}, in f{i}\n    call{i}()\n' for i in range(300)
    )
    trace = f"Traceback (most recent call last):\n{frames}ValueError: final boom\n"
    body = ctx.build_body(make_event(stacktrace=trace), "abc", "v2", "", max_stacktrace_chars=900)
    section = _stack_section(body)
    assert "Traceback (most recent call last):" in section
    assert '  File "/app/src/mod299.py", line 299, in f299' in section
    assert "ValueError: final boom" in section
    assert "characters omitted" in section
    assert len(section) < 1100


def test_truncate_middle_cuts_on_line_boundaries_and_counts_what_it_drops():
    text = "\n".join(f"line {i:04}" for i in range(1000))
    out = ctx.truncate_middle(text, 300)
    head, marker, tail = out.partition("\n... [")
    assert all(line.startswith("line ") and len(line) == 9 for line in head.splitlines())
    tail_lines = tail.split("\n", 1)[1].splitlines()
    assert all(len(line) == 9 for line in tail_lines)
    assert tail_lines[-1] == "line 0999"
    omitted = int(tail.split(" characters omitted")[0])
    assert omitted == len(text) - len(head) - len("\n".join(tail_lines))


def test_truncate_middle_leaves_short_text_alone():
    assert ctx.truncate_middle("short", 100) == "short"
    assert ctx.truncate_middle(None, 10) == ""


def test_regression_comment_keeps_the_tail_of_a_long_stack():
    trace = "head\n" + "middle\n" * 1000 + "Caused by: root.Cause: here"
    comment = ctx.build_occurrence_comment(
        make_event(stacktrace=trace), count=2, regression=True, max_stacktrace_chars=300
    )
    assert "Caused by: root.Cause: here" in comment


# -- at-a-glance rows ----------------------------------------------------------


def _row(body: str, name: str) -> str | None:
    for row in _table_rows(body):
        if row.startswith(f"| {name} |"):
            return row
    return None


GLANCE = ("Location", "Environment", "Host", "Request", "Escaped")


def test_glance_rows_are_absent_without_their_attributes():
    body = ctx.build_body(make_event(attributes={}, resource_attributes={}), "abc", "v2", "")
    for name in GLANCE:
        assert _row(body, name) is None, name


def test_glance_rows_from_current_semconv_names():
    event = make_event(
        attributes={
            "code.file.path": "src/cart.py",
            "code.line.number": "88",
            "code.function.name": "total",
            "http.request.method": "POST",
            "http.route": "/checkout",
            "http.response.status_code": "500",
            "exception.escaped": "true",
        },
        resource_attributes={
            "deployment.environment.name": "production",
            "k8s.pod.name": "checkout-7d9f",
            "k8s.namespace.name": "shop",
            "host.name": "node-1",
        },
    )
    body = ctx.build_body(event, "abc", "v2", "")
    assert _row(body, "Location") == "| Location | `src/cart.py:88 in total` |"
    assert _row(body, "Environment") == "| Environment | `production` |"
    assert _row(body, "Host") == "| Host | `checkout-7d9f (ns shop)` |"
    assert _row(body, "Request") == "| Request | `POST /checkout → 500` |"
    assert _row(body, "Escaped") == "| Escaped | yes (unhandled) |"


def test_glance_rows_from_older_semconv_names():
    event = make_event(
        attributes={
            "code.filepath": "src/cart.py",
            "code.lineno": "88",
            "code.function": "total",
            "http.method": "GET",
            "http.target": "/cart?id=1",
            "http.status_code": "502",
        },
        resource_attributes={"deployment.environment": "staging", "host.name": "vm-3"},
    )
    body = ctx.build_body(event, "abc", "v2", "")
    assert _row(body, "Location") == "| Location | `src/cart.py:88 in total` |"
    assert _row(body, "Environment") == "| Environment | `staging` |"
    assert _row(body, "Host") == "| Host | `vm-3` |"
    assert _row(body, "Request") == "| Request | `GET /cart?id=1 → 502` |"
    assert _row(body, "Escaped") is None


def test_record_attributes_win_over_resource_attributes():
    event = make_event(
        attributes={"deployment.environment": "canary"},
        resource_attributes={"deployment.environment.name": "production"},
    )
    assert _row(ctx.build_body(event, "abc", "v2", ""), "Environment") == (
        "| Environment | `canary` |"
    )


def test_escaped_row_needs_a_true_value():
    event = make_event(attributes={"exception.escaped": "false"})
    assert _row(ctx.build_body(event, "abc", "v2", ""), "Escaped") is None


def test_partial_location_and_request_render_what_exists():
    event = make_event(attributes={"code.function.name": "total", "url.path": "/x"})
    body = ctx.build_body(event, "abc", "v2", "")
    assert _row(body, "Location") == "| Location | `total` |"
    assert _row(body, "Request") == "| Request | `/x` |"


def test_occurrence_comment_carries_environment_and_host():
    event = make_event(
        resource_attributes={"deployment.environment.name": "production", "host.name": "vm-3"}
    )
    comment = ctx.build_occurrence_comment(event, count=2)
    assert "- **Environment** `production`" in comment
    assert "- **Host** `vm-3`" in comment
    bare = ctx.build_occurrence_comment(make_event(resource_attributes={}), count=2)
    assert "Environment" not in bare and "Host" not in bare


def test_footer_says_later_occurrences_become_comments():
    body = ctx.build_body(make_event(), "abc", "v2", "")
    assert "each later occurrence is recorded as a comment" in body


# -- trace backend links ----------------------------------------------------

TEMPLATE = "https://grafana.example.com/explore?traceId={trace_id}"


def test_trace_id_links_to_the_backend_when_configured():
    body = ctx.build_body(make_event(), "abc", "v2", "", trace_url_template=TEMPLATE)
    trace = "4bf92f3577b34da6a3ce929d0e0e4736"
    assert (
        f"| Trace ID | [`{trace}`](https://grafana.example.com/explore?traceId={trace}) |" in body
    )


def test_trace_id_is_plain_code_without_a_template():
    body = ctx.build_body(make_event(), "abc", "v2", "")
    assert "| Trace ID | `4bf92f3577b34da6a3ce929d0e0e4736` |" in body


def test_a_non_hex_trace_id_is_never_put_in_a_url():
    ref = ctx.trace_ref("x) [evil](https://e", TEMPLATE)
    assert ref.startswith("`") and "grafana" not in ref


def test_occurrence_comment_links_the_trace():
    comment = ctx.build_occurrence_comment(make_event(), count=2, trace_url_template=TEMPLATE)
    assert "(https://grafana.example.com/explore?traceId=4bf92f" in comment


# -- AI summary sanitisation -------------------------------------------------


def test_summary_mentions_do_not_notify_anyone():
    out = ctx.sanitize_summary("Ping @alice and @acme/oncall now.")
    assert out == "Ping `@alice` and `@acme/oncall` now."


def test_summary_issue_references_do_not_create_backlinks():
    out = ctx.sanitize_summary("Same as #12 and acme/api#3.")
    assert out == "Same as `#12` and `acme/api#3`."


def test_summary_emails_and_url_fragments_are_not_mangled():
    text = "Mail ops@example.com; see https://docs.example.com/page#12 for details."
    assert ctx.sanitize_summary(text) == text


def test_summary_links_and_images_are_flattened_so_the_target_is_visible():
    out = ctx.sanitize_summary("[fix it](https://evil.example/x) ![p](https://t.example/p.png)")
    assert out == "fix it (https://evil.example/x) p (https://t.example/p.png)"


def test_summary_html_and_forged_headers_are_escaped():
    forged = ctx.machine_header("ab" * 6, "v2", 999)
    out = ctx.sanitize_summary(f"<img src=x> {forged}")
    assert "<" not in out
    assert ctx.parse_header(out) is None


def test_summary_code_spans_are_left_verbatim():
    text = "Guard `total += item.price` against `None`; see `@decorator` and `#1`."
    assert ctx.sanitize_summary(text) == text


def test_summary_sanitisation_is_idempotent_and_capped():
    text = "@a #1 [x](y) <b> `c` " * 5
    once = ctx.sanitize_summary(text)
    assert ctx.sanitize_summary(once) == once
    capped = ctx.sanitize_summary("x" * 5000)
    assert len(capped) <= ctx.MAX_SUMMARY_CHARS + 2 and capped.endswith("…")


def test_body_sanitises_the_summary_it_is_given():
    body = ctx.build_body(make_event(), "ab" * 6, "v2", "Ask @alice about it.")
    assert "`@alice`" in body
    assert "Ask @alice" not in body
