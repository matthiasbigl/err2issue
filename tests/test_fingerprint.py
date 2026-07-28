"""The fingerprint is the identity contract. These tests are what freeze it.

Two properties matter and they pull against each other:
  STABILITY  — the same bug hashes identically across releases, machines, runs
  DISTINCTNESS — different bugs hash differently

Every test below is one or the other. If a change to the normalization rules
breaks one of these, that is not a test to update — it is a signal to ship a new
fingerprint VERSION (see docs/FINGERPRINT.md).
"""

from __future__ import annotations

import pytest

from err2issue import fingerprint as fp
from tests.conftest import JAVA_TRACE, NODE_TRACE, PY_TRACE, make_event

# --------------------------------------------------------------------------
# STABILITY
# --------------------------------------------------------------------------


def test_line_numbers_do_not_change_identity():
    """Adding a line above the fault must not re-file the bug."""
    before = make_event(stacktrace=PY_TRACE)
    after = make_event(stacktrace=PY_TRACE.replace("line 142", "line 157"))
    assert fp.compute(before) == fp.compute(after)


def test_build_path_prefix_does_not_change_identity():
    """CI build dirs differ every run; the last two path segments are kept."""
    before = make_event(stacktrace=PY_TRACE)
    after = make_event(stacktrace=PY_TRACE.replace("/build/9f2a1c/", "/build/0000ff/"))
    assert fp.compute(before) == fp.compute(after)


def test_memory_addresses_do_not_change_identity():
    trace = 'File "/app/src/x.py", line 3, in f\n    <Foo object at 0x7f8a3c2d1e40>'
    other = 'File "/app/src/x.py", line 3, in f\n    <Foo object at 0x55d1aa009900>'
    assert fp.compute(make_event(stacktrace=trace)) == fp.compute(make_event(stacktrace=other))


def test_message_variance_does_not_change_identity_when_a_stack_exists():
    """The stack decides identity; per-request values in the message do not."""
    a = make_event(exception_message="user 8891 not found")
    b = make_event(exception_message="user 4412 not found")
    assert fp.compute(a) == fp.compute(b)


def test_uuid_in_frame_is_normalized():
    base = 'File "/tmp/{}/src/app/run.py", line 9, in go'
    a = make_event(stacktrace=base.format("6f0a1b2c-3d4e-5f60-7181-92a3b4c5d6e7"))
    b = make_event(stacktrace=base.format("00000000-1111-2222-3333-444444444444"))
    assert fp.compute(a) == fp.compute(b)


def test_javascript_line_and_column_are_normalized():
    a = make_event(stacktrace=NODE_TRACE)
    b = make_event(stacktrace=NODE_TRACE.replace(":22:17", ":31:4"))
    assert fp.compute(a) == fp.compute(b)


def test_fingerprint_is_deterministic_across_calls():
    event = make_event()
    assert len({fp.compute(event) for _ in range(20)}) == 1


# --------------------------------------------------------------------------
# DISTINCTNESS
# --------------------------------------------------------------------------


def test_different_exception_types_are_distinct():
    a = make_event(exception_type="TypeError")
    b = make_event(exception_type="ValueError")
    assert fp.compute(a) != fp.compute(b)


def test_different_services_are_distinct():
    """The same library bug in two services is two pieces of work, in two repos."""
    a = make_event(service_name="checkout-api")
    b = make_event(service_name="billing-api")
    assert fp.compute(a) != fp.compute(b)


def test_different_functions_in_the_same_file_are_distinct():
    a = make_event(stacktrace='File "/app/src/x.py", line 10, in handle_checkout\n  boom()')
    b = make_event(stacktrace='File "/app/src/x.py", line 10, in handle_refund\n  boom()')
    assert fp.compute(a) != fp.compute(b)


def test_same_filename_in_different_directories_is_distinct():
    """Keeping two path segments preserves app/handler.py vs worker/handler.py."""
    a = make_event(stacktrace='File "/srv/app/handler.py", line 5, in go\n  x()')
    b = make_event(stacktrace='File "/srv/worker/handler.py", line 5, in go\n  x()')
    assert fp.compute(a) != fp.compute(b)


# --------------------------------------------------------------------------
# FRAME EXTRACTION across ecosystems
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("trace", "expected_fragment"),
    [
        (PY_TRACE, "handlers.py"),
        (JAVA_TRACE, "ProfileService.java"),
        (NODE_TRACE, "user.js"),
        ("/app/x.rb:12:in `handler'\n/app/y.rb:3:in `<main>'", "x.rb"),
        # Go prints the function line before the file line. The function line is
        # the better anchor: it carries the symbol, and its pointer arguments
        # normalize to 0xADDR, whereas the file line also carries a +0x offset
        # that shifts on every recompile.
        (
            "goroutine 1 [running]:\nmain.handler(0x1, 0x2)\n\t/app/svc/main.go:41 +0x1a",
            "main.handler",
        ),
    ],
)
def test_top_frame_skips_the_exception_header(trace, expected_fragment):
    """The header line is not a frame — picking it would fingerprint the message."""
    frame = fp.top_frame(trace)
    assert frame is not None
    assert expected_fragment in frame


def test_top_frame_skips_traceback_preamble():
    assert "Traceback" not in (fp.top_frame(PY_TRACE) or "")


def test_top_frame_skips_caused_by_and_more_markers():
    trace = "Caused by: java.lang.IllegalStateException\n\tat com.acme.A.b(A.java:9)\n\t... 12 more"
    assert "A.java" in (fp.top_frame(trace) or "")


def test_top_frame_of_empty_stack_is_none():
    assert fp.top_frame(None) is None
    assert fp.top_frame("") is None
    assert fp.top_frame("   \n\n  ") is None


# --------------------------------------------------------------------------
# NO-STACK FALLBACK — the case PLAN.md left undefined
# --------------------------------------------------------------------------


def test_no_stacktrace_falls_back_to_normalized_message():
    a = make_event(stacktrace=None, exception_message="connection to db-7 timed out after 3000ms")
    b = make_event(stacktrace=None, exception_message="connection to db-7 timed out after 9000ms")
    assert fp.compute(a) == fp.compute(b)


def test_no_stacktrace_still_distinguishes_different_messages():
    a = make_event(stacktrace=None, exception_message="connection to db timed out")
    b = make_event(stacktrace=None, exception_message="permission denied reading config")
    assert fp.compute(a) != fp.compute(b)


def test_no_stacktrace_quoted_values_are_normalized():
    a = make_event(stacktrace=None, exception_message="unknown column 'user_id' in table")
    b = make_event(stacktrace=None, exception_message="unknown column 'tenant_id' in table")
    assert fp.compute(a) == fp.compute(b)


def test_event_with_neither_stack_nor_message_still_fingerprints():
    event = make_event(stacktrace=None, exception_message="", body=None)
    assert len(fp.compute(event)) == fp.DIGEST_CHARS


def test_fallback_uses_body_when_message_is_empty():
    """A severity-only error with no exception attributes still fingerprints,
    and per-occurrence magnitudes do not split it into many issues."""
    event = make_event(stacktrace=None, exception_message="", body="disk full: 15032385536 bytes")
    other = make_event(stacktrace=None, exception_message="", body="disk full: 15032385999 bytes")
    assert fp.compute(event) == fp.compute(other)


def test_fallback_body_still_distinguishes_different_failures():
    a = make_event(stacktrace=None, exception_message="", body="disk full on data volume")
    b = make_event(stacktrace=None, exception_message="", body="upstream returned 503")
    assert fp.compute(a) != fp.compute(b)


# --------------------------------------------------------------------------
# LABEL CONTRACT
# --------------------------------------------------------------------------


def test_label_round_trips():
    digest = fp.compute(make_event())
    label = fp.label_for(digest)
    assert fp.parse_label(label) == (fp.VERSION, digest)


def test_label_fits_githubs_50_character_limit():
    label = fp.label_for("f" * fp.DIGEST_CHARS)
    assert len(label) <= 50, "GitHub rejects label names longer than 50 characters"


def test_label_carries_the_version_so_rules_can_change():
    """CHALLENGE.md §5: without this, a rules change orphans every issue."""
    digest = "abc123def456"
    assert fp.label_for(digest, "v1") != fp.label_for(digest, "v2")
    assert fp.parse_label(fp.label_for(digest, "v2")) == ("v2", digest)


def test_parse_label_rejects_foreign_labels():
    for label in ("bug", "err2issue", "err2issue-fp-abc", "err2issue-fp-v1-xyz"):
        assert fp.parse_label(label) is None


def test_digest_is_lowercase_hex_of_expected_length():
    digest = fp.compute(make_event())
    assert len(digest) == fp.DIGEST_CHARS
    assert set(digest) <= set("0123456789abcdef")


# --------------------------------------------------------------------------
# GOLDEN VECTOR — locks v1 against accidental drift
# --------------------------------------------------------------------------


def test_v1_golden_vector():
    """If this fails, the v1 rules changed. Ship v2 rather than editing this."""
    event = make_event(
        service_name="checkout-api",
        exception_type="TypeError",
        stacktrace='  File "/app/src/cart.py", line 88, in total\n    return sum(x)',
    )
    parts = fp.fingerprint_parts(event)
    assert parts[0] == "checkout-api"
    assert parts[1] == "TypeError"
    assert parts[2] == 'frame:File "src/cart.py", line L, in total'
