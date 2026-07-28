"""Output modes: direct REST (default), workflow_dispatch (optional), dry-run."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from err2issue.config import Settings
from err2issue.github.auth import StaticTokenProvider
from err2issue.github.client import GitHubClient, RepoUnavailable
from err2issue.github.filer import IssueFiler
from err2issue.sinks import (
    WORKFLOW_INPUT_MAX_CHARS,
    DryRunSink,
    GitHubSink,
    WorkflowDispatchSink,
    build_sink,
)
from tests.conftest import FakeClock, issue_payload, make_event, no_sleep

API = "https://api.github.com"
REPO = "acme/api"
DISPATCH_URL = f"{API}/repos/{REPO}/actions/workflows/file-error-issue.yml/dispatches"


def build_client(http: httpx.AsyncClient) -> GitHubClient:
    return GitHubClient(API, StaticTokenProvider("t"), client=http, sleep=no_sleep)


# -- dry run ---------------------------------------------------------------


async def test_dry_run_records_the_action_without_writing():
    sink = DryRunSink()
    result = await sink.deliver(make_event(), "abc123def456", REPO, "A title")
    assert result.action == "dry-run"
    assert sink.calls[0]["repo"] == REPO
    assert sink.calls[0]["title"] == "[x1] A title"
    assert sink.calls[0]["label"] == "err2issue-fp-v2-abc123def456"


async def test_dry_run_emits_through_the_supplied_callback():
    emitted = []
    sink = DryRunSink(emit=emitted.append)
    await sink.deliver(make_event(), "abc", REPO, "t")
    assert emitted and emitted[0]["repo"] == REPO


# -- workflow_dispatch -----------------------------------------------------


@respx.mock
async def test_workflow_sink_dispatches_with_the_documented_inputs():
    route = respx.post(DISPATCH_URL).mock(return_value=httpx.Response(204))
    async with httpx.AsyncClient() as http:
        sink = WorkflowDispatchSink(build_client(http), ref="main")
        result = await sink.deliver(make_event(), "abc123def456", REPO, "A title")

    body = json.loads(route.calls[0].request.content)
    assert body["ref"] == "main"
    inputs = body["inputs"]
    assert inputs["fingerprint"] == "abc123def456"
    assert inputs["fingerprint_version"] == "v2"
    assert inputs["service"] == "checkout-api"
    assert inputs["exception_type"] == "TypeError"
    assert inputs["title"] == "[x1] A title"
    assert "err2issue:" in inputs["context"]
    assert result.action == "created"


@respx.mock
async def test_workflow_sink_stays_within_the_dispatch_input_budget():
    """Oversized inputs are rejected outright by GitHub, so truncate first."""
    respx.post(DISPATCH_URL).mock(return_value=httpx.Response(204))
    route = respx.post(DISPATCH_URL)
    async with httpx.AsyncClient() as http:
        sink = WorkflowDispatchSink(build_client(http))
        await sink.deliver(make_event(stacktrace="frame\n" * 200_000), "abc", REPO, "t")
    inputs = json.loads(route.calls[0].request.content)["inputs"]
    assert len(inputs["context"]) <= WORKFLOW_INPUT_MAX_CHARS + 100
    assert len(inputs) <= 10, "workflow_dispatch accepts at most 10 inputs"


@respx.mock
async def test_workflow_sink_cannot_report_an_issue_number():
    """204 No Content is the transport's limit, not an omission here."""
    respx.post(DISPATCH_URL).mock(return_value=httpx.Response(204))
    async with httpx.AsyncClient() as http:
        result = await WorkflowDispatchSink(build_client(http)).deliver(
            make_event(), "abc", REPO, "t"
        )
    assert result.number is None
    assert "no issue number" in result.detail


@respx.mock
async def test_workflow_sink_uses_the_configured_file_and_ref():
    url = f"{API}/repos/{REPO}/actions/workflows/custom.yml/dispatches"
    route = respx.post(url).mock(return_value=httpx.Response(204))
    async with httpx.AsyncClient() as http:
        sink = WorkflowDispatchSink(build_client(http), workflow_file="custom.yml", ref="release")
        await sink.deliver(make_event(), "abc", REPO, "t")
    assert json.loads(route.calls[0].request.content)["ref"] == "release"


# -- github sink -----------------------------------------------------------


@respx.mock
async def test_github_sink_delegates_to_the_filer():
    respx.get(f"{API}/repos/{REPO}/issues").mock(return_value=httpx.Response(200, json=[]))
    respx.post(f"{API}/repos/{REPO}/labels").mock(return_value=httpx.Response(201, json={}))
    respx.post(f"{API}/repos/{REPO}/issues").mock(
        return_value=httpx.Response(201, json=issue_payload(number=3))
    )
    async with httpx.AsyncClient() as http:
        from err2issue.github.filer import IssueFiler

        sink = GitHubSink(IssueFiler(build_client(http), sleep=no_sleep))
        result = await sink.deliver(make_event(), "abc123def456", REPO, "t")
    assert result.action == "created"
    assert result.number == 3


# -- selection -------------------------------------------------------------


def test_build_sink_selects_dry_run():
    assert build_sink(Settings(sink="dry-run"), None).name == "dry-run"


def test_build_sink_selects_github_by_default():
    settings = Settings(sink="github", github_token="t", github_repo="a/b")
    sink = build_sink(settings, client=object())
    assert sink.name == "github"


def test_build_sink_selects_workflow():
    settings = Settings(sink="workflow", github_token="t", github_repo="a/b")
    assert build_sink(settings, client=object()).name == "workflow"


def test_build_sink_refuses_a_credentialless_github_sink():
    settings = Settings(sink="github", github_token="t", github_repo="a/b")
    with pytest.raises(ValueError, match="credentials"):
        build_sink(settings, client=None)


def test_extra_labels_are_parsed_from_settings():
    settings = Settings(issue_labels="err2issue, production , triage")
    assert settings.extra_labels == ["err2issue", "production", "triage"]


async def test_github_sink_reports_filer_availability_state():
    """/metrics and /stats read repository availability through the sink."""
    async with httpx.AsyncClient() as http:
        filer = IssueFiler(build_client(http), unavailable_cooldown_seconds=60, clock=FakeClock())
        sink = GitHubSink(filer)
        assert sink.health() == {"unavailable_repos": {}, "unavailable_events": 0}

        filer._mark_unavailable(REPO, RepoUnavailable("issues disabled"))
        assert sink.health() == {"unavailable_repos": {REPO: 60.0}, "unavailable_events": 1}
