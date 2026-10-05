"""Bounded, in-process session store for the survey-as-a-tool primitive (begin_survey/answer_survey).

The MCP connector holds live `Conversation` objects keyed by an opaque session token. Two facts about
how agents use this primitive shape the store:

  • An agent may call `begin_survey` and never finish — it changes its mind mid-task, or the person
    walks away. Without a bound, those abandoned conversations accumulate for the entire life of the
    process. So the store is **capped and evicts the least-recently-used session** when full: abandoned
    sessions are reclaimed, while active ones (touched by each `answer_survey`) stay resident.

  • Sessions live in **this process's** memory. `begin_survey` and its follow-up `answer_survey` calls
    must land on the same worker. A single-process stdio connector is always fine; a multi-worker remote
    deployment must pin a session to its worker (sticky routing) or swap in a shared session store. See
    docs/PRIMITIVE.md.

No third-party deps (in particular, not the `mcp` SDK), so it is unit-testable on its own.
"""
from collections import OrderedDict

DEFAULT_MAX_SESSIONS = 256


class BoundedSessionStore:
    """A dict-like, LRU-bounded map of session_token -> Conversation.

    Supports the subset the server uses: ``store[token] = convo``, ``store.get(token)``,
    ``store.pop(token, default)``, ``token in store``, ``len(store)``. Reads and writes mark the entry
    most-recently-used; once ``max_sessions`` is exceeded, the least-recently-used entry is dropped.
    """

    def __init__(self, max_sessions=DEFAULT_MAX_SESSIONS):
        self.max_sessions = max(1, int(max_sessions))
        self._d = OrderedDict()

    def __setitem__(self, key, value):
        if key in self._d:
            self._d.move_to_end(key)
        self._d[key] = value
        while len(self._d) > self.max_sessions:
            self._d.popitem(last=False)          # evict least-recently-used

    def get(self, key, default=None):
        if key in self._d:
            self._d.move_to_end(key)             # touch: this session is active
            return self._d[key]
        return default

    def pop(self, key, default=None):
        return self._d.pop(key, default)

    def __contains__(self, key):
        return key in self._d

    def __len__(self):
        return len(self._d)
