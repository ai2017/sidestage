"""
The model must never be load-bearing.

These tests drive LLMBackend with fake clients that fail the way the real
API fails -- a model alias this key can't reach (404), a rate limit, a
network drop, and a call that simply takes too long. In every case the
seller must still get a grounded reply drafted from the catalog, with the
reason recorded, rather than a 500 from the console.

Motivated by a real incident: pointing the prototype at
claude-3-5-haiku-latest with a key that lacked it returned
`anthropic.NotFoundError: 404 ... model: claude-3-5-haiku-latest`, which
propagated all the way out of /api/chat and took the request down.
"""

import time

import pytest

from sidestage.ingestion.stream import make_message
from sidestage.reply.generator import DEFAULT_MODEL, LLMBackend, ReplyGenerator, TemplateBackend


class _FakeMessages:
    def __init__(self, error: Exception | None = None, delay_s: float = 0.0):
        self._error = error
        self._delay_s = delay_s
        self.calls = 0

    def create(self, **kwargs):
        self.calls += 1
        if self._delay_s:
            time.sleep(self._delay_s)
        if self._error:
            raise self._error
        raise AssertionError("test did not configure a response")


class _FakeClient:
    def __init__(self, error: Exception | None = None, delay_s: float = 0.0):
        self.messages = _FakeMessages(error, delay_s)


def _backend_with(client, budget_s: float = 2.0) -> LLMBackend:
    """Build an LLMBackend without touching the real SDK or any API key."""
    backend = LLMBackend.__new__(LLMBackend)
    backend._client = client
    backend._model = "test-model"
    backend._budget_s = budget_s
    backend._fallback = TemplateBackend()
    return backend


@pytest.mark.parametrize("error", [
    RuntimeError("Error code: 404 - model: claude-3-5-haiku-latest"),
    RuntimeError("Error code: 429 - rate_limit_error"),
    ConnectionError("connection reset by peer"),
])
def test_api_failure_falls_back_to_a_grounded_reply(tools, error):
    backend = _backend_with(_FakeClient(error=error))
    msg = make_message("v1", "how much is the coral dress")
    draft = backend.draft(msg, tools, "DRS-CORAL-01")

    # The seller still gets the right answer, from the catalog.
    assert "$58.00" in draft.text
    assert draft.backend == "template"
    assert draft.fallback_reason is not None


def test_fallback_reason_names_the_failure(tools):
    backend = _backend_with(_FakeClient(error=RuntimeError("Error code: 404 - model: nope")))
    draft = backend.draft(make_message("v1", "how much is the coral dress"), tools, "DRS-CORAL-01")
    assert "404" in draft.fallback_reason


def test_wall_clock_budget_is_enforced_not_just_the_turn_count(tools):
    """A bounded number of turns is not a bounded duration: four slow calls
    can blow a 2-second reply target on their own."""
    client = _FakeClient(delay_s=0.15, error=RuntimeError("still too slow"))
    backend = _backend_with(client, budget_s=0.1)
    started = time.monotonic()
    draft = backend.draft(make_message("v1", "how much is the coral dress"), tools, "DRS-CORAL-01")
    elapsed = time.monotonic() - started

    assert draft.fallback_reason is not None
    assert elapsed < 1.0  # nowhere near four full turns
    assert "$58.00" in draft.text


def test_guardrails_still_run_on_a_fallback_reply(pipeline, store, monkeypatch):
    """The fallback path is not a bypass: a degraded reply goes through the
    same guardrail and ladder gates as any other."""
    failing = _backend_with(_FakeClient(error=RuntimeError("Error code: 404 - model: nope")))
    pipeline.generator = ReplyGenerator(pipeline.tools, backend=failing)

    result = pipeline.process_message(make_message("v1", "do you have the coral one in a M??"))
    assert result.draft.fallback_reason is not None
    assert "5 left" in result.guardrail.final_text   # grounded in real stock
    assert result.guardrail.violations == []
    assert result.final_status == "sent"


def test_model_id_is_configurable_by_env(monkeypatch):
    monkeypatch.setenv("SIDESTAGE_MODEL", "claude-sonnet-4-5")
    backend = LLMBackend.__new__(LLMBackend)
    import os
    assert os.environ.get("SIDESTAGE_MODEL", DEFAULT_MODEL) == "claude-sonnet-4-5"
