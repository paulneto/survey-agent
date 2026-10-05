import json
import os

import pytest

import survey_agent.ai as ai
from survey_agent.conversation import Conversation

SURVEY = os.path.join(os.path.dirname(__file__), "..", "surveys", "streaming-usage.survey.json")


@pytest.fixture
def spec():
    with open(SURVEY) as f:
        return json.load(f)


def convo(spec):
    # force the deterministic stub so the test is offline + repeatable
    return Conversation(spec, url_vars={"uid": "1", "rid": "1", "group": "demo"}, llm=ai.StubLLM())


def test_conversation_completes_a_current_user(spec):
    c = convo(spec)
    c.opening()                       # asks consent
    c.reply("yes")                    # -> age
    c.reply("I'm 34")                 # -> streaming usage
    c.reply("I currently use it")     # -> brands
    c.reply("netflix")                # -> best brand (current-user page)
    c.reply("netflix")                # -> attention check
    out = c.reply("strongly agree")   # -> complete
    assert out["done"] is True
    assert out["status"] == "completed"
    # the engine enforced the flow; the answers are real
    assert c.session["answers"]["q_service_use"] == "current"


def test_conversation_accepts_multiselect_answer(spec):
    # regression: a checkbox ("select all that apply") must accept more than one option from a free
    # reply — previously the interpreter only kept a single value and re-asked "didn't quite catch that"
    c = convo(spec)
    c.opening()
    c.reply("yes")                    # -> age
    c.reply("34")                     # -> streaming usage
    c.reply("I currently use it")     # -> q_services (checkbox)
    out = c.reply("Netflix and Max")  # two selections in one reply
    assert out.get("clarify") is not True, out["message"]
    assert sorted(c.session["answers"]["q_services"]) == ["max", "netflix"]


def test_conversation_screens_out_on_no_consent(spec):
    c = convo(spec)
    c.opening()
    out = c.reply("no thanks")
    assert out["done"] is True
    assert out["status"] == "disqualified"


def test_conversation_reasks_on_unparseable_required_answer(spec):
    c = convo(spec)
    c.opening()
    c.reply("yes")                    # -> age (required number)
    out = c.reply("not telling you")  # stub can't extract a number -> clarify, stay on age
    assert out.get("clarify") is True
    assert out["done"] is False


def test_survey_as_a_tool_yields_structured_data(spec):
    # the capability the MCP begin_survey/answer_survey tools wrap: drive a survey to a terminal and
    # hand back the engine's canonical structured record (logic-validated answers, not raw text).
    from survey_agent.engine import export as export_mod
    c = convo(spec)
    c.opening()
    for reply in ["yes", "I'm 34", "I currently use it", "netflix", "netflix"]:
        c.reply(reply)
    out = c.reply("strongly agree")
    assert out["done"] is True and out["status"] == "completed"
    rec = export_mod.response_from_session(c.rt, c.session)
    assert rec["status"] == "Complete"
    assert rec["survey_data"]["q_consent"]["answer"]        # a resolved label, not raw free text
    assert rec["survey_data"]["q_service_use"]["answer"]
    assert "derived" in rec                                  # hidden/calc fields ride along


# ----------------------------------------------------------------- adaptive probing
def _probe_spec(max_probes=2):
    return {"survey": {
        "id": "probe-test", "version": 1, "title": "Probe test",
        "settings": {"url_variables": ["uid", "rid"], "theme": "paper",
                     "ai": {"max_probes": max_probes, "probe_min_words": 6}},
        "pages": [{"id": "p1", "questions": [
            {"id": "q_open", "type": "textarea", "required": True, "probe": True, "title": "How was it?"}]}],
        "actions": [{"type": "complete", "redirect": {"kind": "default"}}]}}


def _pconvo(spec):
    return Conversation(spec, url_vars={"uid": "1", "rid": "1"}, llm=ai.StubLLM())


def test_adaptive_probe_fires_on_thin_open_text():
    from survey_agent.engine import export as export_mod
    c = _pconvo(_probe_spec())
    c.opening()                                    # asks q_open
    out = c.reply("meh")                           # thin -> ephemeral follow-up, NOT terminal
    assert out.get("probe") is True and out["done"] is False
    done = c.reply("because the price went up")    # answers the probe -> page completes
    assert done["done"] is True and done["status"] == "completed"
    # the schema answer is the ORIGINAL (probing never overwrites it); the follow-up is stored derived
    assert c.session["answers"]["q_open"] == "meh"
    rec = export_mod.response_from_session(c.rt, c.session)
    assert rec["probes"][0]["qid"] == "q_open"
    assert rec["probes"][0]["answer"] == "because the price went up"


def test_no_probe_on_a_detailed_answer():
    c = _pconvo(_probe_spec())
    c.opening()
    out = c.reply("the price increase finally pushed me to cancel my subscription entirely")  # not thin
    assert out["done"] is True                     # straight to complete, no follow-up


def test_probe_budget_zero_disables_probing():
    c = _pconvo(_probe_spec(max_probes=0))
    c.opening()
    out = c.reply("meh")                           # thin, but no budget -> no probe
    assert out["done"] is True
