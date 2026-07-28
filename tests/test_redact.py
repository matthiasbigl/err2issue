"""Redaction runs before anything reaches GitHub (CHALLENGE.md §7).

Two failure modes are tested with equal weight: leaking a secret (catastrophic,
because the destination is a public-by-default issue tracker) and masking
ordinary text (corrupts every stack trace, so the feature gets turned off).
"""

from __future__ import annotations

import pytest

from err2issue.redact import MASK, Redactor
from tests.conftest import PY_TRACE, make_event

# Every fixture is assembled from fragments rather than written as one literal.
# These have to look exactly like the real thing for the patterns to be worth
# testing, which means a contiguous literal would trip GitHub's push protection
# and block the commit — the scanner reads the file, not the evaluated Python.
# `AKIAIOSFODNN7EXAMPLE` is the exception: it is AWS's published documentation
# key and is allowlisted by the scanner precisely so tests can use it.
SECRETS = [
    ("github classic PAT", "ghp_" + "A" * 36),
    ("github App token", "ghs_" + "B" * 36),
    ("github fine-grained", "github_pat_" + "C" * 30),
    ("AWS access key", "AKIAIOSFODNN7EXAMPLE"),
    ("Slack bot token", "xoxb-" + "1" * 12 + "-" + "a" * 16),
    ("Anthropic key", "sk-ant-api03-" + "d" * 40),
    ("Google API key", "AIza" + "E" * 35),
    ("Stripe live key", "sk_live_" + "f" * 24),
    (
        "JWT",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ),
]


@pytest.mark.parametrize(("name", "secret"), SECRETS, ids=[s[0] for s in SECRETS])
def test_known_token_shapes_are_masked(name, secret):
    redactor = Redactor()
    out = redactor(f"request failed with credential {secret} on retry")
    assert secret not in out, f"{name} leaked"
    assert MASK in out


def test_authorization_header_is_masked():
    out = Redactor()("Headers: {Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345}")
    assert "abcdefghijklmnopqrstuvwxyz012345" not in out


def test_generic_assignment_forms_are_masked():
    redactor = Redactor()
    for text in (
        'api_key="hunter2-supersecret"',
        "password: correct-horse-battery",
        "client_secret=abc123def456ghi",
        "ACCESS_TOKEN = 'zzzzzzzzzzzz'",
    ):
        assert MASK in redactor(text), text


def test_connection_string_keeps_shape_but_masks_password():
    out = Redactor()("could not connect to postgres://appuser:s3cr3tp4ss@db.internal:5432/app")
    assert "s3cr3tp4ss" not in out
    # The host and user are diagnostic and must survive — masking the whole URL
    # would make the error useless.
    assert "db.internal" in out
    assert "appuser" in out


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://postgres:postgres@pg-rw:5432/grid_app",
        "redis://default:default@dragonfly:6379",
        "amqp://admin:admin@rabbit:5672/%2f",
        "mongodb://root:root@mongo:27017/?authSource=admin",
    ],
)
def test_connection_string_password_is_masked_even_when_it_equals_the_username(dsn):
    """The default-credential shapes: `postgres:postgres`, `default:default`.

    Replacing the password by *text* masks the first occurrence in the match,
    which is the username when the two are equal — the password then ships in
    cleartext into a permanent, watcher-emailed issue. The substitution has to
    be positional.
    """
    user, _, rest = dsn.partition("://")[2].partition(":")
    password = rest.split("@")[0]
    out = Redactor()(dsn)
    assert out.endswith(dsn.split("@", 1)[1]), "host and path must survive"
    assert f":{password}@" not in out, f"password leaked: {out}"
    assert f"://{user}:{MASK}@" in out


def test_private_key_block_is_collapsed():
    text = (
        "startup failed\n-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA...\nmore\n-----END RSA PRIVATE KEY-----\nafter"
    )
    out = Redactor()(text)
    assert "MIIEowIBAAKCAQEA" not in out
    assert "startup failed" in out
    assert "after" in out


# -- false positives are just as damaging ----------------------------------


def test_ordinary_stack_traces_are_untouched():
    assert Redactor()(PY_TRACE) == PY_TRACE


def test_ordinary_prose_is_untouched():
    for text in (
        "TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'",
        "GET /api/v1/users/42 returned 500 in 1204ms",
        "could not resolve host analytics.internal",
        "retrying 3 of 5",
    ):
        assert Redactor()(text) == text


def test_short_hex_ids_are_not_mistaken_for_secrets():
    text = "trace_id 4bf92f3577b34da6 span 00f067aa0ba902b7 completed"
    assert Redactor()(text) == text


# -- wiring ----------------------------------------------------------------


def test_disabled_redactor_is_the_identity_function():
    secret = "ghp_" + "Z" * 36
    assert Redactor(enabled=False)(secret) == secret


def test_none_becomes_empty_string_not_a_crash():
    assert Redactor()(None) == ""
    assert Redactor(enabled=False)(None) == ""


def test_custom_patterns_are_applied():
    redactor = Redactor(extra_patterns=[r"INTERNAL-[0-9]{6}"])
    assert MASK in redactor("leaked INTERNAL-998877 here")


def test_invalid_custom_pattern_fails_loudly_at_construction():
    """Better to refuse to start than to silently run without a rule."""
    with pytest.raises(ValueError, match="invalid"):
        Redactor(extra_patterns=["([unclosed"])


def test_scan_reports_which_rules_fired():
    names = Redactor().scan("token ghp_" + "A" * 36)
    assert "github_token" in names


def test_event_redaction_covers_every_free_text_field():
    secret = "ghp_" + "Q" * 36
    event = make_event(
        exception_message=f"auth failed with {secret}",
        stacktrace=f'File "/app/x.py", line 1, in f\n  use({secret})',
        body=f"body {secret}",
        attributes={"http.header.authorization": f"Bearer {secret}"},
        resource_attributes={"deploy.key": f"key={secret}"},
    )
    cleaned = event.with_redactions(Redactor())
    blob = "\n".join(
        [
            cleaned.exception_message,
            cleaned.stacktrace or "",
            cleaned.body or "",
            *cleaned.attributes.values(),
            *cleaned.resource_attributes.values(),
        ]
    )
    assert secret not in blob


def test_redaction_does_not_mutate_the_original_event():
    event = make_event(exception_message="ghp_" + "R" * 36)
    event.with_redactions(Redactor())
    assert "ghp_" in event.exception_message, "ErrorEvent must stay immutable"
