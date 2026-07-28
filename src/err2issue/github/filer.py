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
    ):
        self.client = client
        self.extra_labels = extra_labels or ["err2issue"]
        self.reopen_closed = reopen_closed
        self.max_message_chars = max_message_chars
        self.max_stacktrace_chars = max_stacktrace_chars
        self.max_log_lines = max_log_lines
        self._budget = _CommentBudget(max_comments_per_issue_per_hour)
        self._sleep = sleep
        self._unavailable: set[str] = set()

    async def file(
        self,
        event: ErrorEvent,
        fingerprint: str,
        repo: str,
        summary: str,
        correlated: list[LogLine] | None = None,
    ) -> FiledIssue:
        if repo in self._unavailable:
            return FiledIssue(
                action="skipped",
                fingerprint=fingerprint,
                repo=repo,
                detail="repository previously marked unavailable",
            )

        label = fp.label_for(fingerprint)
        try:
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
                    "label %s exists on %s but no issue carries it; "
                    "treating as orphaned and creating",
                    label,
                    repo,
                )

            return await self._create(event, fingerprint, repo, summary, correlated, label)

        except RepoUnavailable as exc:
            self._unavailable.add(repo)
            log.warning("repository %s unavailable, dropping future errors for it: %s", repo, exc)
            return FiledIssue(action="skipped", fingerprint=fingerprint, repo=repo, detail=str(exc))

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
