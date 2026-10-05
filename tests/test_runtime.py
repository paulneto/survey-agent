import json
import os

import pytest

from survey_agent.engine.runtime import SurveyRuntime

SURVEY = os.path.join(os.path.dirname(__file__), "..", "surveys", "streaming-usage.survey.json")
THANKS_URL = "https://example.com/thanks"


@pytest.fixture
def rt():
    with open(SURVEY) as f:
        return SurveyRuntime(json.load(f))


def drive(rt, answers, url_vars=None, quota_state=None, enrichments=None):
    """Walk the survey, answering each visible question from `answers` (by question id).
    Returns (terminal_dict, list_of_page_ids_seen, final_session)."""
    quota_state = quota_state if quota_state is not None else rt.initial_quota_state()
    session = rt.new_session("t:1", url_vars or {"uid": "1", "rid": "2", "group": "demo"})
    if enrichments:
        session["enrichments"] = enrichments
    step = rt.start(session, quota_state)
    seen = []
    guard = 0
    while "page" in step and guard < 50:
        guard += 1
        page = step["page"]
        seen.append(page["page_id"])
        page_answers = {q["id"]: answers[q["id"]] for q in page["questions"] if q["id"] in answers}
        step = rt.submit(session, page["page_id"], page_answers, quota_state)
    return step["terminal"], seen, session


def test_forward_only_blocks_back_navigation(rt):
    """Browser Back must not let a respondent re-answer a page they've already passed — otherwise
    they could shop for answers that slip past a screen-out. Re-submitting a passed page is a no-op
    that re-renders the page they're actually on; the earlier answer is NOT overwritten."""
    answers = {
        "q_consent": "yes", "q_age": 34, "q_service_use": "current",
        "q_services": ["netflix", "max"], "q_best_service": "netflix", "q_attention": "sa",
    }
    quota_state = rt.initial_quota_state()
    session = rt.new_session("t:fwd", {"uid": "1", "rid": "2", "group": "demo"})
    step = rt.start(session, quota_state)
    page_a = step["page"]
    a_answers = {q["id"]: answers[q["id"]] for q in page_a["questions"] if q["id"] in answers}
    assert a_answers, "fixture survey's first page should have answerable questions"
    step = rt.submit(session, page_a["page_id"], a_answers, quota_state)
    assert "page" in step, "should advance to a second page"
    page_b_id = step["page"]["page_id"]
    pointer_before = session["page_pointer"]
    answers_snapshot = dict(session["answers"])

    # Hit Back: re-submit page A with a tampered answer.
    changed_qid = next(iter(a_answers))
    tampered = dict(a_answers)
    tampered[changed_qid] = "tampered-value"
    step2 = rt.submit(session, page_a["page_id"], tampered, quota_state)

    assert step2.get("page", {}).get("page_id") == page_b_id, "re-renders the current page, not the back page"
    assert session["page_pointer"] == pointer_before, "pointer must not move backward"
    assert session["answers"] == answers_snapshot, "a passed answer must not be overwritten via Back"


def test_current_user_completes_and_redirects(rt):
    terminal, seen, session = drive(rt, {
        "q_consent": "yes", "q_age": 34, "q_service_use": "current",
        "q_services": ["netflix", "max"], "q_best_service": "netflix", "q_attention": "sa",
    }, url_vars={"uid": "u-1001", "rid": "r-2001", "group": "demo"})
    assert terminal["status"] == "completed"
    assert "p_detail" in seen
    r = terminal["redirect"]
    assert r["kind"] == "url"
    assert r["url"] == THANKS_URL


def test_webhook_action_fires_for_current_user():
    # a webhook action fires once when someone is a current subscriber, with uid piped
    with open(SURVEY) as f:
        spec = json.load(f)
    survey = spec.get("survey", spec)
    survey["actions"].insert(0, {
        "type": "webhook",
        "when": {"ref": {"kind": "question", "id": "q_service_use"}, "operator": "equals", "value": "current"},
        "url": "https://hook.example/lead", "method": "POST",
        "payload": {"event": "qualified_lead", "uid": "{{url:uid}}", "usage": "{{q:q_service_use.answer}}"},
    })
    rt = SurveyRuntime(spec)
    qs = rt.initial_quota_state()
    s = rt.new_session("streaming-usage:r:c", {"uid": "u-1001", "rid": "1", "group": "demo"})
    rt.start(s, qs)
    rt.submit(s, "p_intro", {"q_consent": "yes"}, qs)
    step = rt.submit(s, "p_screen", {"q_age": 30, "q_service_use": "current"}, qs)
    hooks = step.get("webhooks")
    assert hooks and hooks[0]["payload"]["uid"] == "u-1001"
    assert hooks[0]["payload"]["usage"] == "Currently subscribe"


def test_piped_options_answer_exports_label_not_raw_value(rt):
    # q_best_service's options are piped from q_services (options_source). Its export label must come
    # from the source question's value→label map, not fall back to the raw stored value ("netflix").
    from survey_agent.engine import export
    terminal, _, session = drive(rt, {
        "q_consent": "yes", "q_age": 40, "q_service_use": "current",
        "q_services": ["netflix", "max"], "q_best_service": "netflix", "q_attention": "sa",
    })
    assert terminal["status"] == "completed"
    resp = export.response_from_session(rt, session)
    assert resp["survey_data"]["q_best_service"]["answer"] == "Netflix"


def test_hidden_calc_values_compute_and_export(rt):
    from survey_agent.engine import export
    terminal, _, session = drive(rt, {
        "q_consent": "yes", "q_age": 40, "q_service_use": "current", "q_services": ["netflix"],
        "q_best_service": "netflix", "q_attention": "sa",
    })
    assert terminal["status"] == "completed"
    assert session["variables"]["stream_score"] == 3
    assert session["variables"]["user_segment"] == "active"
    resp = export.response_from_session(rt, session)
    assert resp["derived"]["stream_score"] == 3
    assert resp["derived"]["user_segment"] == "active"


def test_no_consent_disqualifies(rt):
    terminal, seen, _ = drive(rt, {"q_consent": "no"})
    assert terminal["status"] == "disqualified"
    assert terminal["reason_code"] == "no_consent"
    assert seen == ["p_intro"]


def test_never_user_disqualifies(rt):
    terminal, _, _ = drive(rt, {"q_consent": "yes", "q_age": 40, "q_service_use": "never"})
    assert terminal["status"] == "disqualified"
    assert terminal["reason_code"] == "non_user"


def test_failed_attention_disqualifies(rt):
    terminal, seen, _ = drive(rt, {
        "q_consent": "yes", "q_age": 40, "q_service_use": "current",
        "q_services": ["disney"], "q_best_service": "disney", "q_attention": "n",
    })
    assert terminal["status"] == "disqualified"
    assert terminal["reason_code"] == "failed_attention"
    assert "p_detail" in seen


def test_past_user_skips_detail_page_and_completes(rt):
    terminal, seen, _ = drive(rt, {
        "q_consent": "yes", "q_age": 50, "q_service_use": "past6", "q_services": ["netflix"],
    })
    assert terminal["status"] == "completed"
    assert "p_detail" not in seen  # page display_logic gated to current users only


def test_option_piping_on_detail_page(rt):
    quota_state = rt.initial_quota_state()
    session = rt.new_session("t:pipe", {"uid": "1", "rid": "2", "group": "demo"})
    rt.start(session, quota_state)
    rt.submit(session, "p_intro", {"q_consent": "yes"}, quota_state)
    step = rt.submit(session, "p_screen", {"q_age": 30, "q_service_use": "current"}, quota_state)
    assert step["page"]["page_id"] == "p_brands"
    step = rt.submit(session, "p_brands", {"q_services": ["netflix", "max"]}, quota_state)
    assert step["page"]["page_id"] == "p_detail"
    best = next(q for q in step["page"]["questions"] if q["id"] == "q_best_service")
    assert {o["value"] for o in best["options"]} == {"netflix", "max"}


def test_overquota_when_current_bucket_full(rt):
    quota_state = rt.initial_quota_state()
    quota_state["quota_current"]["count"] = quota_state["quota_current"]["limit"]  # bucket full
    terminal, _, _ = drive(rt, {
        "q_consent": "yes", "q_age": 40, "q_service_use": "current",
        "q_services": ["netflix"], "q_best_service": "netflix", "q_attention": "sa",
    }, quota_state=quota_state)
    assert terminal["status"] == "overquota"


def test_enrichment_drives_behavior_page_and_segment(rt):
    enr = {"stream_search_count_30d": 3, "recent_stream_search": "disney plus vs max",
           "ent_app_minutes_7d": 400}
    terminal, seen, session = drive(rt, {
        "q_consent": "yes", "q_age": 34, "q_service_use": "current", "q_services": ["netflix"],
        "q_why_search": "Wanted to compare prices.", "q_best_service": "netflix", "q_attention": "sa",
    }, enrichments=enr)
    assert terminal["status"] == "completed"
    assert "p_behavior" in seen            # behavior page shown because search_count > 0
    assert session["variables"].get("engaged") == "high"   # enrichment drove a variable


def test_enrichment_piped_into_question_title(rt):
    qs = rt.initial_quota_state()
    s = rt.new_session("t:enrich", {"uid": "1", "rid": "2"})
    s["enrichments"] = {"stream_search_count_30d": 2, "recent_stream_search": "what to watch this weekend"}
    rt.start(s, qs)
    rt.submit(s, "p_intro", {"q_consent": "yes"}, qs)
    rt.submit(s, "p_screen", {"q_age": 41, "q_service_use": "current"}, qs)
    step = rt.submit(s, "p_brands", {"q_services": ["disney"]}, qs)
    assert step["page"]["page_id"] == "p_behavior"
    title = step["page"]["questions"][0]["title"]
    assert "what to watch this weekend" in title    # behavioral fact piped into the "why" question


def test_behavior_page_hidden_without_enrichment(rt):
    terminal, seen, _ = drive(rt, {
        "q_consent": "yes", "q_age": 40, "q_service_use": "current", "q_services": ["netflix"],
        "q_best_service": "netflix", "q_attention": "sa",
    })
    assert terminal["status"] == "completed"
    assert "p_behavior" not in seen


def test_resume_prefills_answers(rt):
    quota_state = rt.initial_quota_state()
    session = rt.new_session("t:resume", {"uid": "1", "rid": "2", "group": "demo"})
    rt.start(session, quota_state)
    rt.submit(session, "p_intro", {"q_consent": "yes"}, quota_state)
    again = rt.current_page(session)
    assert again["page"]["page_id"] == "p_screen"
    assert session["answers"]["q_consent"] == "yes"


def test_randomization_is_deterministic_per_session(rt):
    def order(session_id):
        s = rt.new_session(session_id, {"uid": "1", "rid": "2"})
        rt.start(s, rt.initial_quota_state())
        rt.submit(s, "p_intro", {"q_consent": "yes"}, rt.initial_quota_state())
        page = rt.current_page(s)["page"]
        q = next(q for q in page["questions"] if q["id"] == "q_service_use")
        return [o["value"] for o in q["options"]]
    assert order("seed-A") == order("seed-A")          # stable within a session id
    assert order("seed-A")[-1] == "never"               # 'never' is anchored last


def test_set_variable_value_from_keeps_coded_value_not_label():
    # value_from must copy the STORED (coded) answer, preserving lists — not a piped label string —
    # so a variable derived from an answer stays comparable in later logic (equals / is_one_of).
    spec = {"survey": {
        "id": "sv", "title": "SV", "settings": {"url_variables": ["uid"]},
        "pages": [
            {"id": "p1", "questions": [
                {"id": "q_use", "type": "radio", "required": True,
                 "options": [{"value": "current", "label": "Currently subscribe"},
                             {"value": "never", "label": "Never have"}]},
                {"id": "q_apps", "type": "checkbox",
                 "options": [{"value": "nflx", "label": "Netflix"}, {"value": "max", "label": "Max"}]},
            ]},
            {"id": "p2", "questions": [{"id": "q_end", "type": "text"}]},
        ],
        "actions": [
            {"type": "set_variable", "name": "seg", "value_from": "q_use"},
            {"type": "set_variable", "name": "apps", "value_from": "q_apps"},
            {"type": "complete", "redirect": {"kind": "default"}},
        ],
    }}
    rt = SurveyRuntime(spec)
    qs = rt.initial_quota_state()
    s = rt.new_session("sv:1", {"uid": "1"})
    rt.start(s, qs)
    rt.submit(s, "p1", {"q_use": "current", "q_apps": ["nflx", "max"]}, qs)
    assert s["variables"]["seg"] == "current"          # coded value, NOT "Currently subscribe"
    assert s["variables"]["apps"] == ["nflx", "max"]   # list preserved, NOT "Netflix, Max"
    # and the coded variable is usable in logic downstream
    from survey_agent.engine.ast_eval import EvalContext, evaluate
    ctx = EvalContext(variables=s["variables"])
    assert evaluate({"ref": {"kind": "variable", "id": "seg"}, "operator": "equals", "value": "current"}, ctx)
    assert evaluate({"ref": {"kind": "variable", "id": "apps"}, "operator": "is_one_of", "value": ["max"]}, ctx)
