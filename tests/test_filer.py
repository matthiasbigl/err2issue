"""Filing: dedup, occurrence counting, regression reopen, and the label mutex.

This is where "exactly one issue per unique error" is actually enforced, so
these tests carry the most weight in the suite.
"""

from __future__ import annotations

import json

import httpx
import respx

from err2issue import fingerprint as fp
from err2issue.github.auth import StaticTokenProvider
from err2issue.github.client import GitHubClient
from err2issue.github.filer import IssueFiler
from tests.conftest import issue_payload, make_event, make_log_line, no_sleep

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
