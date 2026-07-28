"""Thin async GitHub REST client — only the endpoints err2issue needs.

Two deliberate choices, both from CHALLENGE.md:

* `list_issues_by_label` uses `GET /repos/{o}/{r}/issues?labels=…&state=all`,
  the issues table, **not** `GET /search/issues`. The search index is eventually
  consistent, capped at 30 req/min and 1,000 results, and can silently return
  partial results — none of which is acceptable for a dedup source of truth.
* `create_label` returns whether *this* caller created the label, turning
  GitHub's atomic create-or-422 into a distributed mutex.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

from .auth import TokenProvider

log = logging.getLogger(__name__)

RETRY_STATUS = {500, 502, 503, 504}
DEFAULT_TIMEOUT = 20.0
MAX_ATTEMPTS = 4


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class RepoUnavailable(GitHubError):
    """Repo is archived, has Issues disabled, or is not visible to our token.

    Distinct from a transient failure: retrying will never help, so the caller
    drops the event and logs once rather than looping.
    """


class GitHubClient:
    def __init__(
        self,
        api_url: str,
        token_provider: TokenProvider,
        client: httpx.AsyncClient | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        sleep=asyncio.sleep,
    ):
        self.api_url = api_url.rstrip("/")
        self.tokens = token_provider
        self._client = client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self._owns_client = client is None
        self.max_attempts = max_attempts
        self._sleep = sleep

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
        await self.tokens.aclose()

    # -- transport ---------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        repo: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        allow_status: set[int] | None = None,
    ) -> httpx.Response:
        url = f"{self.api_url}{path}"
        allow_status = allow_status or set()
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            headers = {
                **await self.tokens.headers_for(repo),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "err2issue",
            }
            try:
                response = await self._client.request(
                    method, url, headers=headers, json=json, params=params
                )
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    raise GitHubError(f"{method} {path} failed: {exc}") from exc
                await self._backoff(attempt)
                continue

            if response.status_code < 400 or response.status_code in allow_status:
                return response

            if response.status_code in (403, 429) and self._is_rate_limited(response):
                delay = self._retry_after(response)
                if attempt == self.max_attempts:
                    raise GitHubError(
                        f"{method} {path}: rate limited and out of retries",
                        status=response.status_code,
                        body=response.text[:500],
                    )
                log.warning(
                    "github rate limited on %s %s; sleeping %.1fs (attempt %d/%d)",
                    method,
                    path,
                    delay,
                    attempt,
                    self.max_attempts,
                )
                await self._sleep(delay)
                continue

            if response.status_code in RETRY_STATUS:
                if attempt == self.max_attempts:
                    raise GitHubError(
                        f"{method} {path}: {response.status_code} after {attempt} attempts",
                        status=response.status_code,
                        body=response.text[:500],
                    )
                await self._backoff(attempt)
                continue

            # 4xx that is not rate limiting: not retryable.
            raise GitHubError(
                f"{method} {path}: {response.status_code} {response.text[:300]}",
                status=response.status_code,
                body=response.text[:500],
            )

        raise GitHubError(f"{method} {path} exhausted retries: {last_error}")

    @staticmethod
    def _is_rate_limited(response: httpx.Response) -> bool:
        if response.status_code == 429:
            return True
        if response.headers.get("x-ratelimit-remaining") == "0":
            return True
        body = response.text.lower()
        return "rate limit" in body or "secondary rate" in body

    def _retry_after(self, response: httpx.Response) -> float:
        """Honour `retry-after`, then `x-ratelimit-reset`, else back off."""
        retry_after = response.headers.get("retry-after")
        if retry_after:
            try:
                return min(float(retry_after), 120.0)
            except ValueError:
                pass
        reset = response.headers.get("x-ratelimit-reset")
        if reset:
            try:
                import time

                return max(1.0, min(float(reset) - time.time(), 120.0))
            except ValueError:
                pass
        return 30.0

    async def _backoff(self, attempt: int) -> None:
        # Exponential with full jitter: 1s, 2s, 4s ... capped.
        delay = min(2 ** (attempt - 1), 8) * (0.5 + random.random() / 2)
        await self._sleep(delay)

    # -- endpoints ---------------------------------------------------------

    async def get_repo(self, repo: str) -> dict[str, Any]:
        response = await self.request("GET", f"/repos/{repo}", repo, allow_status={404})
        if response.status_code == 404:
            raise RepoUnavailable(f"{repo} not found or not visible to this credential", status=404)
        return response.json()

    async def assert_repo_writable(self, repo: str) -> None:
        data = await self.get_repo(repo)
        if data.get("archived"):
            raise RepoUnavailable(f"{repo} is archived; issues cannot be created")
        if data.get("has_issues") is False:
            raise RepoUnavailable(f"{repo} has Issues disabled")

    async def list_issues_by_label(
        self, repo: str, label: str, state: str = "all", per_page: int = 100
    ) -> list[dict[str, Any]]:
        """Consistent dedup lookup. Excludes pull requests.

        GitHub's issues endpoints return PRs too ("every pull request is an
        issue"), so anything carrying a `pull_request` key is filtered out.
        """
        response = await self.request(
            "GET",
            f"/repos/{repo}/issues",
            repo,
            params={"labels": label, "state": state, "per_page": per_page},
            allow_status={410},
        )
        if response.status_code == 410:
            raise RepoUnavailable(f"{repo} has Issues disabled", status=410)
        return [item for item in response.json() if "pull_request" not in item]

    async def create_label(
        self, repo: str, name: str, color: str = "B60205", description: str = ""
    ) -> bool:
        """Create a label. Returns True if *this* call created it.

        GitHub arbitrates: exactly one concurrent caller gets 201, the rest get
        422 `already_exists`. That makes this a mutex with no coordination
        service — see CHALLENGE.md §4.
        """
        response = await self.request(
            "POST",
            f"/repos/{repo}/labels",
            repo,
            json={"name": name, "color": color, "description": description[:100]},
            allow_status={422},
        )
        if response.status_code == 422:
            return False
        return response.status_code == 201

    async def create_issue(
        self, repo: str, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]:
        response = await self.request(
            "POST",
            f"/repos/{repo}/issues",
            repo,
            json={"title": title, "body": body, "labels": labels},
            allow_status={410},
        )
        if response.status_code == 410:
            raise RepoUnavailable(f"{repo} has Issues disabled", status=410)
        return response.json()

    async def update_issue(
        self,
        repo: str,
        number: int,
        title: str | None = None,
        state: str | None = None,
        state_reason: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        if state is not None:
            payload["state"] = state
        if state_reason is not None:
            payload["state_reason"] = state_reason
        response = await self.request("PATCH", f"/repos/{repo}/issues/{number}", repo, json=payload)
        return response.json()

    async def add_comment(self, repo: str, number: int, body: str) -> dict[str, Any]:
        response = await self.request(
            "POST", f"/repos/{repo}/issues/{number}/comments", repo, json={"body": body}
        )
        return response.json()

    async def dispatch_workflow(
        self, repo: str, workflow: str, ref: str, inputs: dict[str, str]
    ) -> None:
        """Fire `workflow_dispatch`. Returns 204 with no body — see CHALLENGE.md §2."""
        await self.request(
            "POST",
            f"/repos/{repo}/actions/workflows/{workflow}/dispatches",
            repo,
            json={"ref": ref, "inputs": inputs},
        )
