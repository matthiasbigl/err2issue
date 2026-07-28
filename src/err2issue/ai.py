"""AI title and summary — an enhancement, never a dependency.

PLAN.md §5.3 gets this exactly right and it is preserved verbatim as a rule:
if the model is unconfigured, unreachable, slow, or refuses, filing proceeds
with a deterministic title. Every failure path here returns the fallback rather
than raising, so a model outage can never stop an error from being filed.

Moved out of the workflow and into the service (CHALLENGE.md §2): in the plan
this ran inside `file-error-issue.yml`, which put an LLM API key into every
adopting repository's secrets. One service means one key in one place.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from .context import fallback_summary
from .models import ErrorEvent

log = logging.getLogger(__name__)

MAX_TITLE_CHARS = 70

_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": (
                "A specific, human-readable issue title, at most 70 characters. "
                "No prefix, no occurrence count, no trailing period."
            ),
        },
        "summary": {
            "type": "string",
            "description": (
                "Two or three sentences: what failed, the most likely cause given "
                "the stack trace, and where to start looking. No preamble."
            ),
        },
    },
    "required": ["title", "summary"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You write GitHub issue titles and summaries for production errors captured "
    "from OpenTelemetry. Your reader is an engineer or an automated fix agent who "
    "cannot see the telemetry backend — only what you write.\n"
    "Be specific and factual. Name the failing operation and the likely cause. "
    "Never invent file paths, line numbers, or causes that the provided data does "
    "not support; if the cause is genuinely unclear, say what would disambiguate it."
)


@dataclass(frozen=True)
class Enrichment:
    title: str
    summary: str
    source: str  # "ai" | "fallback"


class Enricher:
    """Wraps the Anthropic client. `enabled=False` yields the deterministic path."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-opus-5",
        timeout: float = 20.0,
        enabled: bool = True,
        client=None,
    ):
        self.model = model
        self.timeout = timeout
        self.enabled = bool(enabled and (api_key or client))
        self._client = client
        if self.enabled and client is None:
            try:
                from anthropic import AsyncAnthropic

                self._client = AsyncAnthropic(api_key=api_key, timeout=timeout)
            except Exception as exc:  # missing dep, bad key format, etc.
                log.warning("AI enrichment disabled: could not build client: %s", exc)
                self.enabled = False

    def _fallback(self, event: ErrorEvent) -> Enrichment:
        return Enrichment(
            title=fallback_summary(event, MAX_TITLE_CHARS),
            summary="",
            source="fallback",
        )

    def _prompt(self, event: ErrorEvent) -> str:
        parts = [
            f"Service: {event.service_name}",
            f"Version: {event.service_version or 'unknown'}",
            f"Severity: {event.severity}",
            f"Exception type: {event.exception_type}",
            f"Message: {event.exception_message[:1500]}",
        ]
        if event.stacktrace:
            parts.append(f"\nStack trace:\n{event.stacktrace[:4000]}")
        interesting = {
            k: v for k, v in event.attributes.items() if not k.startswith("exception.") and v
        }
        if interesting:
            rendered = "\n".join(f"  {k}={v[:200]}" for k, v in sorted(interesting.items())[:20])
            parts.append(f"\nAttributes:\n{rendered}")
        return "\n".join(parts)

    async def enrich(self, event: ErrorEvent) -> Enrichment:
        if not self.enabled or self._client is None:
            return self._fallback(event)
        try:
            return await asyncio.wait_for(self._call(event), timeout=self.timeout)
        except TimeoutError:
            log.warning("AI enrichment timed out after %.1fs; using fallback title", self.timeout)
        except Exception as exc:
            log.warning("AI enrichment failed (%s); using fallback title", exc)
        return self._fallback(event)

    async def _call(self, event: ErrorEvent) -> Enrichment:
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=_SYSTEM,
            # Low effort: this is a short, scoped, latency-sensitive summarization,
            # not a reasoning task. Thinking stays on (the default on Opus 5).
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
            messages=[{"role": "user", "content": self._prompt(event)}],
        )

        # A refusal returns HTTP 200 with empty/partial content — check before reading.
        if getattr(response, "stop_reason", None) == "refusal":
            log.warning("AI enrichment refused by safety classifier; using fallback title")
            return self._fallback(event)

        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
        if not text:
            return self._fallback(event)

        data = json.loads(text)
        title = " ".join(str(data.get("title", "")).split())[:MAX_TITLE_CHARS].strip()
        summary = str(data.get("summary", "")).strip()
        if not title:
            return self._fallback(event)
        return Enrichment(title=title, summary=summary, source="ai")
