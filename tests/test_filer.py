"""Filing: dedup, occurrence counting, regression reopen, and the label mutex.

This is where "exactly one issue per unique error" is actually enforced, so
these tests carry the most weight in the suite.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import respx

from err2issue import fingerprint as fp
from err2issue.github.auth import StaticTokenProvider
from err2issue.github.client import GitHubClient
from err2issue.github.filer import IssueFiler
from tests.conftest import FakeClock, issue_payload, make_event, make_log_line, no_sleep

API = "https://api.github.com"
REPO = "acme/api"
FINGERPRINT = "abc123def456"
LABEL = fp.label_for(FINGERPRINT)


def build_filer(http: httpx.AsyncClient, **kwargs) -> IssueFiler:
    client = GitHubClient(API, StaticTokenProvider("t"), client=http, sleep=no_sleep)
    return IssueFiler(client, sleep=no_sleep, **kwargs)


def body_of(route, index: int = 0) -> dict:
    return json.loads(route.calls[index].request.content)


# -- create path -----------------------------------------------------------


@respx.mock
async def test_unknown_error_creates_an_issue():
    respx.get(f"{API}/repos/{REPO}/issues").mock(return_value=httpx.Response(200, json=[]))
    label = respx.post(f"{API}/repos/{REPO}/labels").mock(return_value=httpx.Response(201, json={}))
    create = respx.post(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(201, json=issue_payload(number=12))
    )
    async with httpx.AsyncClient() as http:
        result = await build_filer(http).file(make_event(), FINGERPRINT, REPO, "Cart total fails")

    assert result.action == "created"
    assert result.number == 12
    assert result.count == 1
    assert label.called
    payload = body_of(create)
    assert payload["title"] == "[x1] Cart total fails"
    assert LABEL in payload["labels"]
    assert "err2issue" in payload["labels"]


@respx.mock
async def test_created_issue_body_carries_the_machine_header():
    respx.get(f"{API}/repos/{REPO}/issues").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/repos/{REPO}/labels").mock(return_value=httpx.Response(201, json={}))
    create = respx.post(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(201, json=issue_payload())
    )
    async with httpx.AsyncClient() as http:
        await build_filer(http).file(make_event(), FINGERPRINT, REPO, "summary")

    from err2issue.context import parse_header

    assert parse_header(body_of(create)["body"]) == {
        "fingerprint": FINGERPRINT,
        "version": "v2",
        "count": 1,
    }


@respx.mock
async def test_correlated_log_lines_reach_the_issue_body():
    respx.get(f"{API}/repos/{REPO}/issues").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/repos/{REPO}/labels").mock(return_value=httpx.Response(201, json={}))
    create = respx.post(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(201, json=issue_payload())
    )
    async with httpx.AsyncClient() as http:
        await build_filer(http).file(
            make_event(), FINGERPRINT, REPO, "s", correlated=[make_log_line("GET /checkout")]
        )
    assert "GET /checkout" in body_of(create)["body"]


# -- occurrence path -------------------------------------------------------


@respx.mock
async def test_known_error_bumps_the_count_and_comments_instead_of_creating():
    respx.get(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(
            200, json=[issue_payload(number=7, title="[x4] TypeError in checkout", count=4)]
        )
    )
    patch = respx.patch(f"{API}/repos/{REPO}/issues/7").mock(
        return_value=httpx.Response(200, json=issue_payload())
    )
    comment = respx.post(f"{API}/repos/{REPO}/issues/7/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    create = respx.post(f"{API}/repos/{REPO}/issues")
    async with httpx.AsyncClient() as http:
        result = await build_filer(http).file(make_event(), FINGERPRINT, REPO, "s")

    assert result.action == "commented"
    assert result.count == 5
    assert body_of(patch)["title"] == "[x5] TypeError in checkout"
    assert comment.called
    assert not create.called, "a known error must never create a second issue"


@respx.mock
async def test_count_is_taken_from_the_header_when_the_title_was_edited_by_a_human():
    """Someone retitling the issue must not reset the occurrence count."""
    respx.get(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(
            200, json=[issue_payload(number=7, title="Cart is broken again", count=9)]
        )
    )
    patch = respx.patch(f"{API}/repos/{REPO}/issues/7").mock(
        return_value=httpx.Response(200, json=issue_payload())
    )
    respx.post(f"{API}/repos/{REPO}/issues/7/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    async with httpx.AsyncClient() as http:
        result = await build_filer(http).file(make_event(), FINGERPRINT, REPO, "s")

    assert result.count == 10
    assert body_of(patch)["title"] == "[x10] Cart is broken again"


@respx.mock
async def test_an_open_issue_is_preferred_over_a_closed_one():
    respx.get(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(
            200,
            json=[
                issue_payload(number=1, state="closed"),
                issue_payload(number=2, state="open"),
            ],
        )
    )
    patch = respx.patch(f"{API}/repos/{REPO}/issues/2").mock(
        return_value=httpx.Response(200, json=issue_payload())
    )
    respx.post(f"{API}/repos/{REPO}/issues/2/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    async with httpx.AsyncClient() as http:
        result = await build_filer(http).file(make_event(), FINGERPRINT, REPO, "s")
    assert result.number == 2
    assert patch.called


# -- regression path -------------------------------------------------------


@respx.mock
async def test_closed_issue_is_reopened_with_a_regression_comment():
    respx.get(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(
            200, json=[issue_payload(number=7, state="closed", title="[x3] Cart fails", count=3)]
        )
    )
    patch = respx.patch(f"{API}/repos/{REPO}/issues/7").mock(
        return_value=httpx.Response(200, json=issue_payload())
    )
    comment = respx.post(f"{API}/repos/{REPO}/issues/7/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    async with httpx.AsyncClient() as http:
        result = await build_filer(http).file(make_event(), FINGERPRINT, REPO, "s")

    assert result.action == "reopened"
    payload = body_of(patch)
    assert payload["state"] == "open"
    assert payload["state_reason"] == "reopened"
    assert payload["title"] == "[x4] Cart fails"
    assert "Regression" in body_of(comment)["body"]


@respx.mock
async def test_reopening_can_be_disabled():
    respx.get(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(200, json=[issue_payload(number=7, state="closed")])
    )
    patch = respx.patch(f"{API}/repos/{REPO}/issues/7").mock(
        return_value=httpx.Response(200, json=issue_payload())
    )
    respx.post(f"{API}/repos/{REPO}/issues/7/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    async with httpx.AsyncClient() as http:
        result = await build_filer(http, reopen_closed=False).file(
            make_event(), FINGERPRINT, REPO, "s"
        )
    assert result.action == "commented"
    assert "state" not in body_of(patch)


# -- the label mutex -------------------------------------------------------


@respx.mock
async def test_losing_the_label_race_records_an_occurrence_instead_of_duplicating():
    """CHALLENGE.md §4: two replicas, one issue.

    Both see no issue. One wins label creation; the loser re-queries and finds
    the winner's issue because the lookup is strongly consistent.
    """
    respx.get(f"{API}/repos/{REPO}/issues").mock(
        side_effect=[
            httpx.Response(200, json=[]),  # first look: nothing
            httpx.Response(200, json=[issue_payload(number=5)]),  # after losing: winner's issue
        ]
    )
    respx.post(f"{API}/repos/{REPO}/labels").mock(return_value=httpx.Response(422, json={}))
    patch = respx.patch(f"{API}/repos/{REPO}/issues/5").mock(
        return_value=httpx.Response(200, json=issue_payload())
    )
    respx.post(f"{API}/repos/{REPO}/issues/5/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    create = respx.post(f"{API}/repos/{REPO}/issues")

    async with httpx.AsyncClient() as http:
        result = await build_filer(http).file(make_event(), FINGERPRINT, REPO, "s")

    assert result.action == "commented"
    assert result.number == 5
    assert patch.called
    assert not create.called, "the losing replica must not create a duplicate issue"


@respx.mock
async def test_orphaned_label_from_a_deleted_issue_still_files():
    """The label outlived its issue. Falling through to create is correct."""
    respx.get(f"{API}/repos/{REPO}/issues").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/repos/{REPO}/labels").mock(return_value=httpx.Response(422, json={}))
    create = respx.post(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(201, json=issue_payload(number=99))
    )
    async with httpx.AsyncClient() as http:
        result = await build_filer(http).file(make_event(), FINGERPRINT, REPO, "s")
    assert result.action == "created"
    assert result.number == 99
    assert create.called


# -- comment budget --------------------------------------------------------


@respx.mock
async def test_comments_are_capped_per_issue_but_the_count_still_rises():
    """A long-running error updates [xN] cheaply without spamming the thread."""
    respx.get(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(200, json=[issue_payload(number=7)])
    )
    patch = respx.patch(f"{API}/repos/{REPO}/issues/7").mock(
        return_value=httpx.Response(200, json=issue_payload())
    )
    comment = respx.post(f"{API}/repos/{REPO}/issues/7/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    async with httpx.AsyncClient() as http:
        filer = build_filer(http, max_comments_per_issue_per_hour=2)
        for _ in range(6):
            await filer.file(make_event(), FINGERPRINT, REPO, "s")

    assert comment.call_count == 2, "comment budget must cap the thread"
    assert patch.call_count == 6, "but the occurrence count must still be updated"


@respx.mock
async def test_a_regression_always_comments_even_when_the_budget_is_spent():
    respx.get(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(200, json=[issue_payload(number=7, state="closed")])
    )
    respx.patch(f"{API}/repos/{REPO}/issues/7").mock(
        return_value=httpx.Response(200, json=issue_payload())
    )
    comment = respx.post(f"{API}/repos/{REPO}/issues/7/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    async with httpx.AsyncClient() as http:
        filer = build_filer(http, max_comments_per_issue_per_hour=0)
        await filer.file(make_event(), FINGERPRINT, REPO, "s")
    assert comment.called


# -- unavailable repositories ----------------------------------------------


@respx.mock
async def test_repository_with_issues_disabled_is_skipped_and_remembered():
    """Dropping once and remembering beats retrying forever."""
    listing = respx.get(f"{API}/repos/{REPO}/issues").mock(return_value=httpx.Response(410))
    async with httpx.AsyncClient() as http:
        filer = build_filer(http)
        first = await filer.file(make_event(), FINGERPRINT, REPO, "s")
        second = await filer.file(make_event(), FINGERPRINT, REPO, "s")

    assert first.action == "skipped"
    assert second.action == "skipped"
    assert listing.call_count == 1, "an unavailable repo must not be probed again"


@respx.mock
async def test_unavailable_repository_is_probed_again_after_the_cooldown():
    """ "Issues disabled" is a setting somebody can turn back on.

    Without an expiry, a migration that toggles it for an hour costs every
    error for the lifetime of the pod, which keeps reporting Ready throughout.
    """
    clock = FakeClock()
    listing = respx.get(f"{API}/repos/{REPO}/issues").mock(return_value=httpx.Response(410))
    async with httpx.AsyncClient() as http:
        filer = build_filer(http, unavailable_cooldown_seconds=900, clock=clock)
        assert (await filer.file(make_event(), FINGERPRINT, REPO, "s")).action == "skipped"

        clock.now += 899
        assert (await filer.file(make_event(), FINGERPRINT, REPO, "s")).action == "skipped"
        assert listing.call_count == 1, "still cooling down"

        clock.now += 2
        assert (await filer.file(make_event(), FINGERPRINT, REPO, "s")).action == "skipped"
        assert listing.call_count == 2, "cooldown lapsed, so the repo is probed again"


@respx.mock
async def test_a_repository_that_comes_back_files_again():
    """The whole point of the expiry: recovery without a restart."""
    clock = FakeClock()
    listing = respx.get(f"{API}/repos/{REPO}/issues").mock(
        side_effect=[httpx.Response(410), httpx.Response(200, json=[])]
    )
    respx.post(f"{API}/repos/{REPO}/labels").mock(return_value=httpx.Response(201, json={}))
    respx.post(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(201, json=issue_payload())
    )
    async with httpx.AsyncClient() as http:
        filer = build_filer(http, unavailable_cooldown_seconds=60, clock=clock)
        assert (await filer.file(make_event(), FINGERPRINT, REPO, "s")).action == "skipped"
        assert filer.health()["unavailable_repos"] == {REPO: 60.0}

        clock.now += 61
        assert (await filer.file(make_event(), FINGERPRINT, REPO, "s")).action == "created"

    assert listing.call_count == 2
    assert filer.health()["unavailable_repos"] == {}, "recovery must clear the mark"
    assert filer.health()["unavailable_events"] == 1


@respx.mock
async def test_a_still_broken_repository_re_arms_rather_than_resetting():
    """A lapsed cooldown that finds the repo still gone must not fall through
    to probing on every subsequent error."""
    clock = FakeClock()
    listing = respx.get(f"{API}/repos/{REPO}/issues").mock(return_value=httpx.Response(410))
    async with httpx.AsyncClient() as http:
        filer = build_filer(http, unavailable_cooldown_seconds=60, clock=clock)
        await filer.file(make_event(), FINGERPRINT, REPO, "s")
        clock.now += 61
        await filer.file(make_event(), FINGERPRINT, REPO, "s")
        for _ in range(5):
            clock.now += 10
            await filer.file(make_event(), FINGERPRINT, REPO, "s")

    assert listing.call_count == 2, "the second failure must re-arm the cooldown"
    assert filer.health()["unavailable_events"] == 2


@respx.mock
async def test_one_unavailable_repository_does_not_block_another():
    other = "acme/worker"
    respx.get(f"{API}/repos/{REPO}/issues").mock(return_value=httpx.Response(410))
    respx.get(f"{API}/repos/{other}/issues").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/repos/{other}/labels").mock(return_value=httpx.Response(201, json={}))
    respx.post(f"{API}/repos/{other}/issues").mock(
        return_value=httpx.Response(201, json=issue_payload())
    )
    async with httpx.AsyncClient() as http:
        filer = build_filer(http)
        assert (await filer.file(make_event(), FINGERPRINT, REPO, "s")).action == "skipped"
        assert (await filer.file(make_event(), FINGERPRINT, other, "s")).action == "created"

    assert list(filer.health()["unavailable_repos"]) == [REPO]


# -- lookup shape ----------------------------------------------------------


@respx.mock
async def test_lookup_uses_the_versioned_fingerprint_label():
    listing = respx.get(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(200, json=[])
    )
    respx.post(f"{API}/repos/{REPO}/labels").mock(return_value=httpx.Response(201, json={}))
    respx.post(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(201, json=issue_payload())
    )
    async with httpx.AsyncClient() as http:
        await build_filer(http).file(make_event(), FINGERPRINT, REPO, "s")

    params = listing.calls[0].request.url.params
    assert params["labels"] == f"err2issue-fp-v2-{FINGERPRINT}"
    assert params["state"] == "all"


# -- summary vs description (A1) -------------------------------------------


def _mock_create_path():
    respx.get(f"{API}/repos/{REPO}/issues").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/repos/{REPO}/labels").mock(return_value=httpx.Response(201, json={}))
    return respx.post(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(201, json=issue_payload())
    )


@respx.mock
async def test_no_summary_section_when_there_is_no_description():
    """The contract: `### Summary` is absent when enrichment fell back."""
    create = _mock_create_path()
    async with httpx.AsyncClient() as http:
        await build_filer(http).file(make_event(), FINGERPRINT, REPO, "Cart total fails")
    payload = body_of(create)
    assert payload["title"] == "[x1] Cart total fails"
    assert "### Summary" not in payload["body"]


@respx.mock
async def test_summary_section_carries_the_description_not_the_title():
    create = _mock_create_path()
    async with httpx.AsyncClient() as http:
        await build_filer(http).file(
            make_event(),
            FINGERPRINT,
            REPO,
            "Cart total fails",
            description="Prices can be null after the catalogue import.",
        )
    payload = body_of(create)
    assert payload["title"] == "[x1] Cart total fails"
    body = payload["body"]
    assert "### Summary\n\nPrices can be null after the catalogue import." in body
    assert "Cart total fails" not in body


# -- body refresh on occurrence (A2) ---------------------------------------


def _existing_body(count: int = 3, version: str = "1.4.2") -> str:
    from err2issue import context as ctx

    event = make_event(service_version=version, timestamp=datetime(2026, 7, 1, 9, 0, 0, tzinfo=UTC))
    return ctx.build_body(event, FINGERPRINT, "v1", summary="", count=count)


def _mock_occurrence(body: str | None):
    respx.get(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(
            200, json=[issue_payload(number=7, title="[x3] TypeError in checkout", body=body)]
        )
    )
    respx.post(f"{API}/repos/{REPO}/issues/7/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    return respx.patch(f"{API}/repos/{REPO}/issues/7").mock(
        return_value=httpx.Response(200, json=issue_payload())
    )


@respx.mock
async def test_occurrence_refreshes_header_and_rows_in_the_same_patch():
    from err2issue.context import parse_header

    patch = _mock_occurrence(_existing_body(count=3))
    async with httpx.AsyncClient() as http:
        await build_filer(http).file(make_event(), FINGERPRINT, REPO, "s")

    assert len(patch.calls) == 1, "title, state and body go in one round-trip"
    payload = body_of(patch)
    assert payload["title"] == "[x4] TypeError in checkout"
    body = payload["body"]
    # The issue keeps the version it was filed under; only the count moves.
    assert parse_header(body) == {"fingerprint": FINGERPRINT, "version": "v1", "count": 4}
    assert "| Last seen | 2026-07-28 12:00:00 UTC |" in body
    assert "| Occurrences | 4 |" in body
    assert "| First seen | 2026-07-01 09:00:00 UTC |" in body
    assert "Latest version" not in body


@respx.mock
async def test_occurrence_body_refresh_preserves_human_edits():
    edited = _existing_body().replace(
        "### Exception", "Triage note: owned by payments.\n\n### Exception"
    )
    patch = _mock_occurrence(edited)
    async with httpx.AsyncClient() as http:
        await build_filer(http).file(make_event(), FINGERPRINT, REPO, "s")
    assert "Triage note: owned by payments." in body_of(patch)["body"]


@respx.mock
async def test_a_new_service_version_adds_a_latest_version_row():
    patch = _mock_occurrence(_existing_body(version="1.4.2"))
    async with httpx.AsyncClient() as http:
        await build_filer(http).file(make_event(service_version="1.5.0"), FINGERPRINT, REPO, "s")
    body = body_of(patch)["body"]
    assert "| Version | `1.4.2` |\n| Latest version | `1.5.0` |" in body

    # A later occurrence replaces the row rather than stacking another.
    from err2issue.github.filer import _refresh_body

    again = _refresh_body(body, make_event(service_version="1.6.0"), 5)
    assert again.count("Latest version") == 1
    assert "| Latest version | `1.6.0` |" in again


@respx.mock
async def test_body_without_a_header_is_not_patched():
    patch = _mock_occurrence("A human rewrote this whole body.")
    async with httpx.AsyncClient() as http:
        await build_filer(http).file(make_event(), FINGERPRINT, REPO, "s")
    payload = body_of(patch)
    assert "body" not in payload
    assert payload["title"] == "[x4] TypeError in checkout"


# -- body size cap (A3) ----------------------------------------------------


@respx.mock
async def test_a_huge_log_line_still_files_within_the_body_limit():
    from err2issue.context import parse_header

    create = _mock_create_path()
    huge = make_log_line("x" * 100_000)
    async with httpx.AsyncClient() as http:
        result = await build_filer(http).file(
            make_event(), FINGERPRINT, REPO, "s", correlated=[huge]
        )
    assert result.action == "created"
    body = body_of(create)["body"]
    assert len(body) <= 65_536
    assert parse_header(body)["count"] == 1
    assert "[truncated by err2issue]" in body
    fences = [line for line in body.split("\n") if line.startswith("```")]
    assert len(fences) % 2 == 0, "clipping must not leave a code fence open"


def test_clip_leaves_short_bodies_alone():
    from err2issue.github.filer import _clip

    assert _clip("short") == "short"


def test_clip_closes_an_open_fence():
    from err2issue.github.filer import _clip

    body = "header\n\n```\n" + "line\n" * 1000
    clipped = _clip(body, limit=500)
    assert len(clipped) <= 500
    assert clipped.startswith("header")
    fences = [line for line in clipped.split("\n") if line.startswith("```")]
    assert len(fences) == 2


@respx.mock
async def test_occurrence_comments_are_clipped():
    respx.get(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(200, json=[issue_payload(number=7)])
    )
    respx.patch(f"{API}/repos/{REPO}/issues/7").mock(
        return_value=httpx.Response(200, json=issue_payload())
    )
    comment = respx.post(f"{API}/repos/{REPO}/issues/7/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    async with httpx.AsyncClient() as http:
        await build_filer(http, max_stacktrace_chars=200_000).file(
            make_event(stacktrace="frame\n" * 30_000), FINGERPRINT, REPO, "s"
        )
    assert len(body_of(comment)["body"]) <= 65_536
