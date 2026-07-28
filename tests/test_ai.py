"""AI enrichment. The contract under test is PLAN.md §5.3: never a dependency.

Every failure mode a model call can have — unconfigured, timeout, exception,
refusal, malformed output, empty output — must yield a filed issue with a
deterministic title, not an exception.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from err2issue.ai import Enricher
from tests.conftest import make_event


class FakeMessages:
    def __init__(self, result=None, error: Exception | None = None, delay: float = 0.0):
        self._result = result
        self._error = error
        self._delay = delay
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._result


class FakeClient:
    def __init__(self, **kwargs):
        self.messages = FakeMessages(**kwargs)


class Block:
    def __init__(self, text: str, type: str = "text"):
        self.text = text
        self.type = type


class Response:
    def __init__(self, text: str, stop_reason: str = "end_turn"):
        self.content = [Block(text)]
        self.stop_reason = stop_reason


def good_response(title="Cart total fails on missing price", summary="Because x.") -> Response:
    return Response(json.dumps({"title": title, "summary": summary}))


# -- happy path ------------------------------------------------------------


async def test_model_output_is_used_when_available():
    enricher = Enricher(client=FakeClient(result=good_response()))
    result = await enricher.enrich(make_event())
    assert result.source == "ai"
    assert result.title == "Cart total fails on missing price"
    assert result.summary == "Because x."


async def test_request_uses_structured_output_and_low_effort():
    client = FakeClient(result=good_response())
    await Enricher(client=client).enrich(make_event())
    kwargs = client.messages.calls[0]
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    # A short, latency-sensitive summarization does not need deep reasoning.
    assert kwargs["output_config"]["effort"] == "low"
    assert kwargs["model"] == "claude-opus-5"


async def test_prompt_includes_the_diagnostic_material():
    client = FakeClient(result=good_response())
    await Enricher(client=client).enrich(make_event())
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "checkout-api" in prompt
    assert "TypeError" in prompt
    assert "cart.py" in prompt


async def test_overlong_model_titles_are_trimmed():
    enricher = Enricher(client=FakeClient(result=good_response(title="z" * 300)))
    result = await enricher.enrich(make_event())
    assert len(result.title) <= 70


# -- every failure mode falls back, none raises ----------------------------


async def test_unconfigured_enricher_uses_the_fallback():
    result = await Enricher(api_key=None, enabled=True).enrich(make_event())
    assert result.source == "fallback"
    assert result.title.startswith("TypeError")


async def test_explicitly_disabled_enricher_uses_the_fallback():
    result = await Enricher(client=FakeClient(result=good_response()), enabled=False).enrich(
        make_event()
    )
    assert result.source == "fallback"


async def test_api_exception_falls_back():
    enricher = Enricher(client=FakeClient(error=RuntimeError("503 overloaded")))
    result = await enricher.enrich(make_event())
    assert result.source == "fallback"


async def test_timeout_falls_back():
    enricher = Enricher(client=FakeClient(result=good_response(), delay=0.5), timeout=0.01)
    result = await enricher.enrich(make_event())
    assert result.source == "fallback"


async def test_safety_refusal_falls_back():
    """A refusal returns HTTP 200 with no usable content — check stop_reason."""
    enricher = Enricher(client=FakeClient(result=Response("", stop_reason="refusal")))
    result = await enricher.enrich(make_event())
    assert result.source == "fallback"


async def test_malformed_json_falls_back():
    enricher = Enricher(client=FakeClient(result=Response("not json at all")))
    result = await enricher.enrich(make_event())
    assert result.source == "fallback"


async def test_empty_content_falls_back():
    enricher = Enricher(client=FakeClient(result=Response("")))
    result = await enricher.enrich(make_event())
    assert result.source == "fallback"


async def test_missing_title_field_falls_back():
    enricher = Enricher(client=FakeClient(result=Response(json.dumps({"summary": "s"}))))
    result = await enricher.enrich(make_event())
    assert result.source == "fallback"


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("boom"), ValueError("bad"), ConnectionError("net"), KeyError("k")],
)
async def test_no_exception_type_escapes_the_enricher(failure):
    """Filing must proceed no matter how the model call fails."""
    result = await Enricher(client=FakeClient(error=failure)).enrich(make_event())
    assert result.source == "fallback"
    assert result.title


async def test_fallback_title_is_still_useful():
    event = make_event(exception_type="ValueError", exception_message="invalid tenant id")
    result = await Enricher(enabled=False).enrich(event)
    assert "ValueError" in result.title
    assert "invalid tenant id" in result.title


async def test_enabled_flag_is_false_without_a_key_or_client():
    assert Enricher(api_key=None, enabled=True).enabled is False
    assert Enricher(client=FakeClient(result=good_response()), enabled=True).enabled is True
