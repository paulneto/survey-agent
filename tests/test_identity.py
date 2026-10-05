"""Identity variables are configurable per survey (settings.identity), defaulting to uid/rid.
A deployment can keep using pid/tid (or anything) and everything keyed on identity — the session
key, dedup, enrichment lookup, DSAR — follows the configured names."""
from survey_agent.engine.runtime import SurveyRuntime
from survey_agent.store.memory import MemoryStore


def _survey(identity=None):
    s = {"survey": {
        "id": "idtest", "title": "ID", "version": 1,
        "settings": {"url_variables": ["pid", "tid", "group"]},
        "pages": [{"id": "p1", "questions": [
            {"id": "q1", "type": "radio", "required": True,
             "options": [{"value": "y", "label": "Y"}, {"value": "n", "label": "N"}]}]}],
        "actions": [{"type": "complete", "redirect": {"kind": "url", "url": "https://x/?u={{url:pid}}"}}],
    }}
    if identity:
        s["survey"]["settings"]["identity"] = identity
    return s


def test_defaults_to_uid_rid():
    rt = SurveyRuntime(_survey())
    assert (rt.respondent_var, rt.response_var) == ("uid", "rid")


def test_custom_identity_vars_are_used():
    rt = SurveyRuntime(_survey(identity={"respondent": "pid", "response": "tid"}))
    assert (rt.respondent_var, rt.response_var) == ("pid", "tid")
    # identity() pulls the right values out of url_vars regardless of the name
    respondent, response = rt.identity({"pid": "abc", "tid": "999", "group": "g"})
    assert respondent == "abc" and response == "999"


def test_custom_identity_keys_the_store_session():
    rt = SurveyRuntime(_survey(identity={"respondent": "pid", "response": "tid"}))
    store = MemoryStore()
    url_vars = {"pid": "person-7", "tid": "resp-3", "group": "g"}
    uid, rid = rt.identity(url_vars)
    session, created = store.get_or_create_session(
        "idtest", rid, uid, lambda: rt.new_session(f"idtest:{rid}:{uid}", url_vars))
    assert created and session["session_id"] == "idtest:resp-3:person-7"
    # same identity -> same session (idempotent), not a duplicate
    again, created2 = store.get_or_create_session(
        "idtest", rid, uid, lambda: rt.new_session("SHOULD-NOT-BE-USED", url_vars))
    assert not created2 and again["session_id"] == "idtest:resp-3:person-7"


def test_merge_token_uses_literal_var_name():
    # piping references url vars by literal name, so the redirect uses {{url:pid}} here
    rt = SurveyRuntime(_survey(identity={"respondent": "pid", "response": "tid"}))
    s = rt.new_session("idtest:r:c", {"pid": "u-1001", "tid": "1", "group": "g"})
    rt.start(s, rt.initial_quota_state())
    step = rt.submit(s, "p1", {"q1": "y"}, rt.initial_quota_state())
    assert step["terminal"]["redirect"]["url"] == "https://x/?u=u-1001"
