"""settings.retake — may one invite link (uid+rid) be taken more than once?

Default "once" is the long-standing behavior (a finished link is locked); "multiple" lets a returning
respondent start a fresh take, with each completion kept as its own distinct response + passback.
Response identity is a system-minted `response_id` (minted at session start, independent of the
invite vars), so distinct takes are distinct responses regardless of whether uid/rid repeat. Reuses
the hermetic HTTP harness from test_server_http (single-survey mode; the retake gate lives in the
shared GET handler, so it exercises the same code multi mode uses)."""
import re

from survey_agent.engine import terminal
from survey_agent.engine.runtime import response_id, STATUS_COMPLETED

from test_server_http import running_server, _get, _post


# ----------------------------------------------------------------- unit: response identity
def test_response_id_uses_minted_field():
    # the stored/passback identity is the system-minted response_id, independent of the invite vars
    assert response_id({"session_id": "s:rid:uid", "response_id": "abc123"}) == "abc123"


def test_response_id_falls_back_to_session_id_when_unminted():
    # synthetic / in-memory sessions (simulation, chat preview) don't mint one -> fall back to session_id
    assert response_id({"session_id": "s:rid:uid"}) == "s:rid:uid"
    assert response_id({"session_id": "s:rid:uid", "response_id": None}) == "s:rid:uid"


def test_terminal_event_distinct_per_response_id():
    # two takes of the same link carry different minted response_ids -> distinct response_id AND
    # idempotency_key (so a delivering receiver does NOT dedupe the second take's passback)
    s1 = {"session_id": "screener:r-2001:u-1001", "survey_id": "screener", "url_vars": {},
          "response_id": "r-aaa"}
    s2 = {**s1, "response_id": "r-bbb"}
    e1 = terminal.build_event(s1, STATUS_COMPLETED)
    e2 = terminal.build_event(s2, STATUS_COMPLETED)
    assert e1["response_id"] == "r-aaa" and e2["response_id"] == "r-bbb"
    assert e1["idempotency_key"] != e2["idempotency_key"]


# ----------------------------------------------------------------- HTTP flow
def _complete(base, uid=10, rid=20):
    """Drive the one-question consent survey to completion; return the response HTML."""
    _, html = _get(f"{base}/s/t_http?uid={uid}&rid={rid}&group=demo")
    sid = re.search(r'name=session_id value="([^"]+)"', html).group(1)
    pid = re.search(r'name=page_id value="([^"]+)"', html).group(1)
    _, done = _post(f"{base}/s/t_http/submit", {"session_id": sid, "page_id": pid, "q_consent": "yes"})
    return done


_SPEC = {
    "id": "t_http", "version": 1, "title": "HTTP test",
    "pages": [{"id": "p1", "questions": [
        {"id": "q_consent", "type": "radio", "required": True, "title": "Do you consent?",
         "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}]}]}],
    "actions": [{"type": "complete", "redirect": {"kind": "default"}}],
}


def _spec(**settings):
    base = {"url_variables": ["uid", "rid", "group"], "theme": "base"}
    base.update(settings)
    return {"survey": {**_SPEC, "settings": base}}


def test_retake_once_locks_a_finished_link():
    with running_server(_spec()) as base:               # default policy: no `retake` setting at all
        assert "Complete" in _complete(base)
        # returning on the same link -> the already-completed screen, not the survey
        _, again = _get(f"{base}/s/t_http?uid=10&rid=20&group=demo")
        assert "already completed" in again
        assert "Do you consent?" not in again


def test_partial_session_mints_response_id_and_resume_preserves_it():
    # a response_id is minted at session start (so abandoned/partial responses are addressable), and
    # reopening the same in-progress link resumes the same session + response_id (not a new one)
    with running_server(_spec()) as base:
        import survey_agent.server as server            # same (reloaded) module the server thread uses
        _get(f"{base}/s/t_http?uid=11&rid=20&group=demo")           # start, do not submit
        sess = server.STORE.get_session_by_id("t_http:20:11")
        assert sess and sess.get("response_id") and sess["status"] == "in_progress"
        rid1 = sess["response_id"]
        _get(f"{base}/s/t_http?uid=11&rid=20&group=demo")           # reopen -> resume same response
        sess2 = server.STORE.get_session_by_id("t_http:20:11")
        assert sess2["response_id"] == rid1


def test_retake_multiple_starts_a_fresh_take_each_visit():
    with running_server(_spec(retake="multiple")) as base:
        assert "Complete" in _complete(base)
        # returning on the same link -> the survey again (a fresh take), not the locked screen
        _, again = _get(f"{base}/s/t_http?uid=10&rid=20&group=demo")
        assert "Do you consent?" in again
        assert "already completed" not in again
        assert "Complete" in _complete(base)            # and it can be completed a second time

        import survey_agent.server as server            # same (reloaded) module the server thread uses
        rows = server.STORE.list_responses("t_http")
        completed = [r for r in rows if r.get("status") == "Complete"]
        assert len(completed) == 2                       # two distinct responses, not one overwritten
        ids = {r["id"] for r in completed}
        assert len(ids) == 2                             # each take got its own system-minted response_id
        # both carry the same invite vars as DATA — proving uniqueness is NOT derived from uid/rid
        assert all(r["url_variables"]["uid"] == "10" and r["url_variables"]["rid"] == "20"
                   for r in completed)
