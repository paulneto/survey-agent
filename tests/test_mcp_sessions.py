"""The bounded session store backing begin_survey/answer_survey. Imports only the dep-free
`survey_agent.mcp.sessions` module, so it runs whether or not the `mcp` SDK is installed."""
from survey_agent.mcp.sessions import BoundedSessionStore


def test_get_pop_contains_like_a_dict():
    store = BoundedSessionStore(max_sessions=4)
    store["sv_a"] = "convo-a"
    assert "sv_a" in store
    assert store.get("sv_a") == "convo-a"
    assert store.get("missing") is None
    assert store.pop("sv_a") == "convo-a"
    assert "sv_a" not in store
    assert store.pop("sv_a", "default") == "default"


def test_evicts_least_recently_used_when_full():
    store = BoundedSessionStore(max_sessions=2)
    store["a"] = 1
    store["b"] = 2
    store["c"] = 3                 # over capacity -> oldest ("a") evicted
    assert "a" not in store
    assert "b" in store and "c" in store
    assert len(store) == 2


def test_access_keeps_active_session_resident():
    """An abandoned session is the one that gets reclaimed — touching a session via get() (what
    answer_survey does on every reply) marks it recently-used so it survives eviction."""
    store = BoundedSessionStore(max_sessions=2)
    store["a"] = 1
    store["b"] = 2
    store.get("a")                 # 'a' is active again; 'b' is now the stale one
    store["c"] = 3                 # evicts the least-recently-used -> 'b'
    assert "a" in store
    assert "b" not in store
    assert "c" in store


def test_reassigning_a_token_does_not_grow_or_evict():
    store = BoundedSessionStore(max_sessions=2)
    store["a"] = 1
    store["b"] = 2
    store["a"] = 99                # same key -> update, not a new slot
    assert len(store) == 2
    assert store.get("a") == 99
    assert "b" in store
