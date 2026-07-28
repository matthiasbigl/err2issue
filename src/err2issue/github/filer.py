"""Dedup and file: the piece that makes "exactly one issue per unique error" true.

The algorithm, and why each step is what it is:

1. **Look up by label** against the issues table (`state=all`), not the search
   index. Strongly consistent, so an issue created 200ms ago is visible.
2. **Found** -> occurrence: bump `[xN]`, comment, reopen if it was closed.
3. **Not found** -> claim creation by *creating the label*. GitHub returns 201
   to exactly one caller and 422 to the rest, so this is a mutex arbitrated by
   GitHub's own database with no coordination service.
4. **Lost the claim** -> re-query (consistent, so the winner's issue is
   visible once written) and record an occurrence instead.

Honest bound on step 4: this narrows the duplicate-creation window from
"however long the search index lags" (seconds to minutes) to "one HTTP
round-trip" (~200ms), and the bounded re-query retry closes most of what is
left. It is not a perfect distributed lock — a hard-paused process between
label creation and issue creation can still produce a duplicate. It is a very
large improvement over the design in PLAN.md §5.2, which had no consistent read
at all. See CHALLENGE.md §1 and §4.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime

from .. import context as ctx
from .. import fingerprint as fp
from ..models import ErrorEvent, FiledIssue, LogLine
from .client import GitHubClient, RepoUnavailable

log = logging.getLogger(__name__)

LABEL_COLOR = "B60205"
CLAIM_RETRIES = 3
CLAIM_BACKOFF_SECONDS = 0.4
UNAVAILABLE_COOLDOWN_SECONDS = 900.0


class _CommentBudget:
    """Cap occurrence comments per issue per hour.

    A long-running error should update its count, not generate a comment every
    time. The title `[xN]` is the cheap signal; comments are the expensive one.
    """

    def __init__(self, max_per_hour: int, clock=time.monotonic):
        self.max_per_hour = max_per_hour
        self._clock = clock
        self._seen: OrderedDict[tuple[str, int], list[float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, repo: str, number: int) -> bool:
        if self.max_per_hour <= 0:
            return False
        key = (repo, number)
        now = self._clock()
        with self._lock:
            stamps = [t for t in self._seen.get(key, []) if now - t < 3600]
            if len(stamps) >= self.max_per_hour:
                self._seen[key] = stamps
                return False
            stamps.append(now)
            self._seen[key] = stamps
            self._seen.move_to_end(key)
            while len(self._seen) > 5000:
                self._seen.popitem(last=False)
            return True


class IssueFiler:
    def __init__(
        self,
        client: GitHubClient,
        extra_labels: list[str] | None = None,
        reopen_closed: bool = True,
        max_comments_per_issue_per_hour: int = 4,
        max_message_chars: int = 2000,
        max_stacktrace_chars: int = 6000,
        max_log_lines: int = 20,
        sleep=asyncio.sleep,
        unavailable_cooldown_seconds: float = UNAVAILABLE_COOLDOWN_SECONDS,
        clock=time.monotonic,
    ):
        self.client = client
        self.extra_labels = extra_labels or ["err2issue"]
        self.reopen_closed = reopen_closed
        self.max_message_chars = max_message_chars
        self.max_stacktrace_chars = max_stacktrace_chars
        self.max_log_lines = max_log_lines
        self._budget = _CommentBudget(max_comments_per_issue_per_hour)
        self._sleep = sleep
        self.unavailable_cooldown_seconds = unavailable_cooldown_seconds
        self._clock = clock
        # repo -> monotonic deadline after which we probe it again.
        self._unavailable: dict[str, float] = {}
        self.unavailable_events = 0

    async def file(
        self,
        event: ErrorEvent,
        fingerprint: str,
        repo: str,
        summary: str,
        correlated: list[LogLine] | None = None,
    ) -> FiledIssue:
        cooling = self._cooling_down(repo)
        if cooling is not None:
            return FiledIssue(
                action="skipped",
                fingerprint=fingerprint,
                repo=repo,
                detail=f"repository marked unavailable; retrying in {cooling:.0f}s",
            )

        try:
            result = await self._file(event, fingerprint, repo, summary, correlated)
        except RepoUnavailable as exc:
            self._mark_unavailable(repo, exc)
            return FiledIssue(action="skipped", fingerprint=fingerprint, repo=repo, detail=str(exc))
        self._mark_available(repo)
        return result

    async def _file(
        self,
        event: ErrorEvent,
        fingerprint: str,
        repo: str,
        summary: str,
        correlated: list[LogLine] | None,
    ) -> FiledIssue:
        label = fp.label_for(fingerprint)
        existing = await self.client.list_issues_by_label(repo, label, state="all")
        if existing:
            return await self._record_occurrence(
                self._pick(existing), event, fingerprint, repo, correlated
            )

        claimed = await self.client.create_label(
            repo,
            label,
            color=LABEL_COLOR,
            description=f"err2issue fingerprint {fp.VERSION}:{fingerprint}",
        )
        if not claimed:
            # Another replica is creating, or the label outlived a deleted
            # issue. Re-query with a short bounded backoff before deciding.
            for attempt in range(CLAIM_RETRIES):
                await self._sleep(CLAIM_BACKOFF_SECONDS * (attempt + 1))
                existing = await self.client.list_issues_by_label(repo, label, state="all")
                if existing:
                    return await self._record_occurrence(
                        self._pick(existing), event, fingerprint, repo, correlated
                    )
            log.info(
                "label %s exists on %s but no issue carries it; treating as orphaned and creating",
                label,
                repo,
            )

        return await self._create(event, fingerprint, repo, summary, correlated, label)

    # -- repository availability -------------------------------------------
    #
    # What lands here is a 410: GitHub's answer when a repository has Issues
    # disabled. (A 404 on the issues endpoint stays a plain GitHubError — it is
    # counted as a failure and retried on the next error, which is right, since
    # a 404 there can also mean a permissions blip.) Retrying a 410 once per
    # error would burn quota for nothing, so the first one suppresses the rest.
    #
    # But "Issues disabled" is a repository *setting*, not a fact of nature:
    # somebody turns it off during a migration and back on an hour later.
    # Remembering it until the pod restarts turns a temporary setting into
    # indefinite data loss on a replica that keeps reporting Ready — with one
    # log line, hours earlier, as the only trace. So the memory expires, every
    # lapse re-logs, and /metrics carries a gauge and a counter for it.

    def _cooling_down(self, repo: str) -> float | None:
        """Seconds left on this repo's cooldown, or None if it should be tried."""
        deadline = self._unavailable.get(repo)
        if deadline is None:
            return None
        remaining = deadline - self._clock()
        if remaining > 0:
            return remaining
        # Left in the map until the probe resolves it, so that a still-broken
        # repo re-arms rather than silently reverting to "fine".
        log.info("repository %s cooldown expired; probing again", repo)
        return None

    def _mark_unavailable(self, repo: str, exc: RepoUnavailable) -> None:
        self.unavailable_events += 1
        self._unavailable[repo] = self._clock() + self.unavailable_cooldown_seconds
        log.warning(
            "repository %s unavailable, dropping its errors for %.0fs: %s",
            repo,
            self.unavailable_cooldown_seconds,
            exc,
        )

    def _mark_available(self, repo: str) -> None:
        if self._unavailable.pop(repo, None) is not None:
            log.info("repository %s is reachable again", repo)

    def health(self) -> dict:
        """Availability state, for /stats and /metrics."""
        now = self._clock()
        return {
            "unavailable_repos": {
                repo: round(max(0.0, deadline - now), 1)
                for repo, deadline in self._unavailable.items()
            },
            "unavailable_events": self.unavailable_events,
        }

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _pick(issues: list[dict]) -> dict:
        """Prefer an open issue; otherwise the most recently updated closed one."""
        open_issues = [i for i in issues if i.get("state") == "open"]
        pool = open_issues or issues
        return max(pool, key=lambda i: i.get("updated_at") or "")

    async def _create(
        self,
        event: ErrorEvent,
        fingerprint: str,
        repo: str,
        summary: str,
        correlated: list[LogLine] | None,
        label: str,
    ) -> FiledIssue:
        title = ctx.format_title(1, summary)
        body = ctx.build_body(
            event=event,
            fingerprint=fingerprint,
            version=fp.VERSION,
            summary=summary,
            count=1,
            correlated=correlated,
            max_message_chars=self.max_message_chars,
            max_stacktrace_chars=self.max_stacktrace_chars,
            max_log_lines=self.max_log_lines,
        )
        labels = [*self.extra_labels, label]
        issue = await self.client.create_issue(repo, title=title, body=body, labels=labels)
        return FiledIssue(
            action="created",
            fingerprint=fingerprint,
            repo=repo,
            number=issue.get("number"),
            url=issue.get("html_url"),
            count=1,
        )

    async def _record_occurrence(
        self,
        issue: dict,
        event: ErrorEvent,
        fingerprint: str,
        repo: str,
        correlated: list[LogLine] | None,
    ) -> FiledIssue:
        number = issue["number"]
        title = issue.get("title") or ""
        title_count, stem = ctx.parse_title_count(title)
        header = ctx.parse_header(issue.get("body"))
        header_count = int(header["count"]) if header else 1
        # The title is what humans edit and the header is what we wrote; trust
        # whichever is further along so a manual retitle never loses the count.
        new_count = max(title_count, header_count) + 1

        was_closed = issue.get("state") == "closed"
        regression = was_closed and self.reopen_closed

        await self.client.update_issue(
            repo,
            number,
            title=ctx.format_title(new_count, stem),
            state="open" if regression else None,
            state_reason="reopened" if regression else None,
        )

        # A regression is always worth a comment; routine occurrences are budgeted.
        if regression or self._budget.allow(repo, number):
            await self.client.add_comment(
                repo,
                number,
                ctx.build_occurrence_comment(
                    event,
                    count=new_count,
                    correlated=correlated,
                    regression=regression,
                    max_stacktrace_chars=self.max_stacktrace_chars,
                ),
            )

        return FiledIssue(
            action="reopened" if regression else "commented",
            fingerprint=fingerprint,
            repo=repo,
            number=number,
            url=issue.get("html_url"),
            count=new_count,
        )


def now() -> datetime:
    return datetime.now(UTC)
