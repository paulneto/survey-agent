"""Authoring resilience: a single garbled/truncated model reply must not abort the whole run.

The real providers also pass timeout/max_retries to the SDK (transient network/5xx/429 → retried
in the SDK); that path needs a live client, so here we pin the engine-level repair retry on the
JSON-extraction failure, which is provider-agnostic (it lives in _BaseLLM.author)."""
from survey_agent.ai import _BaseLLM, _llm_timeout, _llm_retries

_VALID = '{"survey": {"id": "t", "title": "T", "pages": [], "actions": [{"type": "complete"}]}}'


class _FlakyLLM(_BaseLLM):
    """First reply is unparseable prose; second is valid JSON."""
    name = "flaky"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def _msg(self, prompt, max_tokens=400):
        self.calls += 1
        return self._replies.pop(0)


def test_author_retries_once_on_unparseable_reply():
    llm = _FlakyLLM(["Sure! Here is your survey: (oops, truncated", _VALID])
    out = llm.author("brief", {})
    assert out["survey"]["id"] == "t"
    assert llm.calls == 2                 # it retried exactly once and recovered


def test_author_does_not_retry_when_first_reply_is_valid():
    llm = _FlakyLLM([_VALID, _VALID])
    out = llm.author("brief", {})
    assert out["survey"]["id"] == "t"
    assert llm.calls == 1                 # no wasted second call on success


def test_author_propagates_when_both_replies_fail():
    import pytest
    llm = _FlakyLLM(["not json", "still not json"])
    with pytest.raises(ValueError):
        llm.author("brief", {})
    assert llm.calls == 2                 # tried, repaired once, then gave up


def test_llm_timeout_and_retries_are_env_tunable(monkeypatch):
    monkeypatch.setenv("SURVEY_LLM_TIMEOUT", "12.5")
    monkeypatch.setenv("SURVEY_LLM_RETRIES", "4")
    assert _llm_timeout() == 12.5
    assert _llm_retries() == 4
    monkeypatch.setenv("SURVEY_LLM_TIMEOUT", "garbage")    # bad value falls back, never raises
    monkeypatch.setenv("SURVEY_LLM_RETRIES", "garbage")
    assert _llm_timeout() == 60.0
    assert _llm_retries() == 2
