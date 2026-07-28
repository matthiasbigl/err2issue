"""REST client: endpoint choice, retry policy, and the label mutex primitive."""

from __future__ import annotations

import httpx
import pytest
import respx

from err2issue.github.auth import StaticTokenProvider
from err2issue.github.client import GitHubClient, GitHubError, RepoUnavailable
from tests.conftest import issue_payload, no_sleep

API = "https://api.github.com"


def build_client(http: httpx.AsyncClient, **kwargs) -> GitHubClient:
    return GitHubClient(API, StaticTokenProvider("ghp_test"), client=http, sleep=no_sleep, **kwargs)


# -- dedup uses the consistent endpoint, not search -------------------------


@respx.mock
async def test_dedup_reads_the_issues_table_not_the_search_index():
    """CHALLENGE.md §1: this is the single most important assertion in the suite.

    `/search/issues` is eventually consistent, rate limited to 30/min, and can
    return partial results. Dedup must never touch it.
    """
    search = respx.get(f"{API}/search/issues")
    issues = respx.get(f"{API}/repos/acme/api/issues").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient() as http:
        await build_client(http).list_issues_by_label("acme/api", "err2issue-fp-v1-abc")
    assert issues.called
    assert not search.called, "dedup must not use the search API"


@respx.mock
async def test_dedup_query_asks_for_open_and_closed_issues():
    """Regression detection depends on seeing closed issues."""
    route = respx.get(f"{API}/repos/acme/api/issues").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient() as http:
        await build_client(http).list_issues_by_label("acme/api", "lbl")
    params = route.calls[0].request.url.params
    assert params["state"] == "all"
    assert params["labels"] == "lbl"


@respx.mock
async def test_pull_requests_are_excluded_from_dedup_results():
    """GitHub's issues endpoints return PRs too; matching one would be wrong."""
    respx.get(f"{API}/repos/acme/api/issues").mock(
        return_value=httpx.Response(
            200,
            json=[
                {**issue_payload(number=1), "pull_request": {"url": "..."}},
                issue_payload(number=2),
            ],
        )
    )
    async with httpx.AsyncClient() as http:
        found = await build_client(http).list_issues_by_label("acme/api", "lbl")
    assert [i["number"] for i in found] == [2]


# -- label as a distributed mutex ------------------------------------------


@respx.mock
async def test_create_label_returns_true_for_the_winner():
    respx.post(f"{API}/repos/acme/api/labels").mock(return_value=httpx.Response(201, json={}))
    async with httpx.AsyncClient() as http:
        assert await build_client(http).create_label("acme/api", "lbl") is True


@respx.mock
async def test_create_label_returns_false_when_it_already_exists():
    """422 already_exists is the losing side of the mutex, not an error."""
    respx.post(f"{API}/repos/acme/api/labels").mock(
        return_value=httpx.Response(422, json={"errors": [{"code": "already_exists"}]})
    )
    async with httpx.AsyncClient() as http:
        assert await build_client(http).create_label("acme/api", "lbl") is False


@respx.mock
async def test_label_description_is_truncated_to_githubs_limit():
    route = respx.post(f"{API}/repos/acme/api/labels").mock(
        return_value=httpx.Response(201, json={})
    )
    async with httpx.AsyncClient() as http:
        await build_client(http).create_label("acme/api", "lbl", description="x" * 500)
    import json as _json

    assert len(_json.loads(route.calls[0].request.content)["description"]) <= 100


# -- retry policy ----------------------------------------------------------


@respx.mock
async def test_transient_5xx_is_retried_then_succeeds():
    respx.get(f"{API}/repos/acme/api/issues").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(502),
            httpx.Response(200, json=[]),
        ]
    )
    async with httpx.AsyncClient() as http:
        assert await build_client(http).list_issues_by_label("acme/api", "lbl") == []


@respx.mock
async def test_persistent_5xx_eventually_raises():
    respx.get(f"{API}/repos/acme/api/issues").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http:
        with pytest.raises(GitHubError):
            await build_client(http, max_attempts=2).list_issues_by_label("acme/api", "lbl")


@respx.mock
async def test_client_errors_are_not_retried():
    """A 404 will never become a 200; retrying just wastes the rate limit."""
    route = respx.get(f"{API}/repos/acme/api/issues").mock(return_value=httpx.Response(400))
    async with httpx.AsyncClient() as http:
        with pytest.raises(GitHubError):
            await build_client(http).list_issues_by_label("acme/api", "lbl")
    assert route.call_count == 1


@respx.mock
async def test_network_errors_are_retried():
    respx.get(f"{API}/repos/acme/api/issues").mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json=[])]
    )
    async with httpx.AsyncClient() as http:
        assert await build_client(http).list_issues_by_label("acme/api", "lbl") == []


# -- rate limiting ---------------------------------------------------------


@respx.mock
async def test_secondary_rate_limit_is_honoured_and_retried():
    """GitHub caps content creation at 80/min and 500/hr; 403 + retry-after."""
    respx.post(f"{API}/repos/acme/api/issues").mock(
        side_effect=[
            httpx.Response(
                403,
                headers={"retry-after": "1"},
                json={"message": "You have exceeded a secondary rate limit"},
            ),
            httpx.Response(201, json=issue_payload()),
        ]
    )
    async with httpx.AsyncClient() as http:
        created = await build_client(http).create_issue("acme/api", "t", "b", ["l"])
    assert created["number"] == 7


@respx.mock
async def test_primary_rate_limit_exhaustion_is_retried():
    respx.get(f"{API}/repos/acme/api/issues").mock(
        side_effect=[
            httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, json={"message": "limit"}),
            httpx.Response(200, json=[]),
        ]
    )
    async with httpx.AsyncClient() as http:
        assert await build_client(http).list_issues_by_label("acme/api", "lbl") == []


@respx.mock
async def test_a_plain_403_is_not_treated_as_rate_limiting():
    """A permissions failure must surface immediately, not retry-loop."""
    route = respx.get(f"{API}/repos/acme/api/issues").mock(
        return_value=httpx.Response(403, json={"message": "Resource not accessible by integration"})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(GitHubError):
            await build_client(http).list_issues_by_label("acme/api", "lbl")
    assert route.call_count == 1


# -- repository availability ------------------------------------------------


@respx.mock
async def test_archived_repository_is_reported_as_unavailable():
    respx.get(f"{API}/repos/acme/api").mock(
        return_value=httpx.Response(200, json={"archived": True, "has_issues": True})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(RepoUnavailable, match="archived"):
            await build_client(http).assert_repo_writable("acme/api")


@respx.mock
async def test_repository_with_issues_disabled_is_reported_as_unavailable():
    respx.get(f"{API}/repos/acme/api").mock(
        return_value=httpx.Response(200, json={"archived": False, "has_issues": False})
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(RepoUnavailable, match="Issues disabled"):
            await build_client(http).assert_repo_writable("acme/api")


@respx.mock
async def test_410_gone_on_issue_creation_means_issues_are_disabled():
    respx.post(f"{API}/repos/acme/api/issues").mock(return_value=httpx.Response(410))
    async with httpx.AsyncClient() as http:
        with pytest.raises(RepoUnavailable):
            await build_client(http).create_issue("acme/api", "t", "b", [])


# -- headers and misc ------------------------------------------------------


@respx.mock
async def test_requests_carry_auth_and_api_version_headers():
    route = respx.get(f"{API}/repos/acme/api/issues").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient() as http:
        await build_client(http).list_issues_by_label("acme/api", "lbl")
    headers = route.calls[0].request.headers
    assert headers["authorization"] == "Bearer ghp_test"
    assert headers["x-github-api-version"] == "2022-11-28"
    assert headers["accept"] == "application/vnd.github+json"


@respx.mock
async def test_enterprise_base_url_is_respected():
    """GHES lives at /api/v3, GHE.com at api.SUBDOMAIN.ghe.com."""
    ghes = "https://ghes.example.com/api/v3"
    route = respx.get(f"{ghes}/repos/acme/api/issues").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with httpx.AsyncClient() as http:
        client = GitHubClient(ghes, StaticTokenProvider("t"), client=http, sleep=no_sleep)
        await client.list_issues_by_label("acme/api", "lbl")
    assert route.called


@respx.mock
async def test_update_issue_sends_only_the_provided_fields():
    import json as _json

    route = respx.patch(f"{API}/repos/acme/api/issues/7").mock(
        return_value=httpx.Response(200, json=issue_payload())
    )
    async with httpx.AsyncClient() as http:
        await build_client(http).update_issue("acme/api", 7, title="new")
    assert _json.loads(route.calls[0].request.content) == {"title": "new"}


@respx.mock
async def test_reopen_sends_state_and_state_reason():
    import json as _json

    route = respx.patch(f"{API}/repos/acme/api/issues/7").mock(
        return_value=httpx.Response(200, json=issue_payload())
    )
    async with httpx.AsyncClient() as http:
        await build_client(http).update_issue("acme/api", 7, state="open", state_reason="reopened")
    body = _json.loads(route.calls[0].request.content)
    assert body["state"] == "open"
    assert body["state_reason"] == "reopened"


@respx.mock
async def test_workflow_dispatch_posts_ref_and_inputs():
    import json as _json

    route = respx.post(
        f"{API}/repos/acme/api/actions/workflows/file-error-issue.yml/dispatches"
    ).mock(return_value=httpx.Response(204))
    async with httpx.AsyncClient() as http:
        await build_client(http).dispatch_workflow(
            "acme/api", "file-error-issue.yml", "main", {"fingerprint": "abc"}
        )
    body = _json.loads(route.calls[0].request.content)
    assert body["ref"] == "main"
    assert body["inputs"]["fingerprint"] == "abc"
