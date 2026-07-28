"""Output modes. The sink is the only part that knows how an issue gets filed.

`github` is the default and the one the design argues for (CHALLENGE.md §2).
`workflow` is kept because PLAN.md §5.2 is right that *anything* being able to
file an issue is valuable — it is just the wrong default. `dry-run` is the
primary test harness, exactly as PLAN.md §8 intended.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod

from . import context as ctx
from . import fingerprint as fp
from .github.client import GitHubClient
from .github.filer import IssueFiler
from .models import ErrorEvent, FiledIssue, LogLine

log = logging.getLogger(__name__)

# workflow_dispatch caps inputs at 10 keys, and oversized values are rejected
# outright rather than truncated — so we truncate first. See CHALLENGE.md §2.
WORKFLOW_INPUT_MAX_CHARS = 60_000


class Sink(ABC):
    name: str = "sink"

    @abstractmethod
    async def deliver(
        self,
        event: ErrorEvent,
        fingerprint: str,
        repo: str,
        summary: str,
        correlated: list[LogLine] | None = None,
    ) -> FiledIssue: ...

    async def aclose(self) -> None:
        return None


class GitHubSink(Sink):
    name = "github"

    def __init__(self, filer: IssueFiler):
        self.filer = filer

    async def deliver(self, event, fingerprint, repo, summary, correlated=None) -> FiledIssue:
        return await self.filer.file(event, fingerprint, repo, summary, correlated)

    async def aclose(self) -> None:
        await self.filer.client.aclose()


class WorkflowDispatchSink(Sink):
    """Fire `workflow_dispatch` and let a workflow do the filing.

    Note the loss of feedback this entails: the endpoint returns 204 with no
    body, so the returned FiledIssue has no number and no URL. That is inherent
    to the transport, not an omission here.
    """

    name = "workflow"

    def __init__(
        self,
        client: GitHubClient,
        workflow_file: str = "file-error-issue.yml",
        ref: str = "main",
        max_message_chars: int = 2000,
        max_stacktrace_chars: int = 6000,
        max_log_lines: int = 20,
    ):
        self.client = client
        self.workflow_file = workflow_file
        self.ref = ref
        self.max_message_chars = max_message_chars
        self.max_stacktrace_chars = max_stacktrace_chars
        self.max_log_lines = max_log_lines

    async def deliver(self, event, fingerprint, repo, summary, correlated=None) -> FiledIssue:
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
        inputs = {
            "fingerprint": fingerprint,
            "fingerprint_version": fp.VERSION,
            "service": event.service_name,
            "exception_type": event.exception_type,
            "occurred_at": event.timestamp.isoformat(),
            "title": ctx.format_title(1, summary),
            "context": ctx.truncate(body, WORKFLOW_INPUT_MAX_CHARS),
        }
        await self.client.dispatch_workflow(repo, self.workflow_file, self.ref, inputs)
        return FiledIssue(
            action="created",
            fingerprint=fingerprint,
            repo=repo,
            detail="dispatched to workflow (no issue number available from this transport)",
        )

    async def aclose(self) -> None:
        await self.client.aclose()


class DryRunSink(Sink):
    """Log every action that would be taken, write nothing. Records for assertions."""

    name = "dry-run"

    def __init__(self, emit=None):
        self.calls: list[dict] = []
        self._emit = emit or (lambda payload: log.info("dry-run: %s", json.dumps(payload)))

    async def deliver(self, event, fingerprint, repo, summary, correlated=None) -> FiledIssue:
        payload = {
            "repo": repo,
            "fingerprint": f"{fp.VERSION}:{fingerprint}",
            "label": fp.label_for(fingerprint),
            "title": ctx.format_title(1, summary),
            "service": event.service_name,
            "exception_type": event.exception_type,
            "correlated_lines": len(correlated or []),
        }
        self.calls.append(payload)
        self._emit(payload)
        return FiledIssue(
            action="dry-run", fingerprint=fingerprint, repo=repo, detail="no write performed"
        )


def build_sink(settings, client: GitHubClient | None) -> Sink:
    if settings.sink == "dry-run":
        return DryRunSink()
    if client is None:
        raise ValueError(f"sink {settings.sink!r} needs GitHub credentials")
    if settings.sink == "workflow":
        return WorkflowDispatchSink(
            client,
            workflow_file=settings.workflow_file,
            ref=settings.workflow_ref,
            max_message_chars=settings.max_message_chars,
            max_stacktrace_chars=settings.max_stacktrace_chars,
            max_log_lines=settings.max_context_log_lines,
        )
    return GitHubSink(
        IssueFiler(
            client,
            extra_labels=settings.extra_labels,
            reopen_closed=settings.reopen_closed,
            max_comments_per_issue_per_hour=settings.max_comment_per_issue_per_hour,
            max_message_chars=settings.max_message_chars,
            max_stacktrace_chars=settings.max_stacktrace_chars,
            max_log_lines=settings.max_context_log_lines,
        )
    )
