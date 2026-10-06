"""Question / page repeating (engine/repeat.py): ask a question or a whole page once per item from an
earlier answer, with each item's answer stored under its own instance id `qid[item]`."""
import copy
import random
import re

from survey_agent.engine.export import response_from_session
from survey_agent.engine.runtime import SurveyRuntime
from survey_agent.engine.validate import validate
from survey_agent.exporting import flatten
from survey_agent.simulate import coverage

BRANDS = [{"value": "netflix", "label": "Netflix"}, {"value": "hulu", "label": "Hulu"},
          {"value": "max", "label": "Max"}, {"value": "none", "label": "None of these", "exclusive": True}]


def _survey(**over):
    s = {"id": "rep", "version": 1, "title": "Repeat",
         "pages": [
             {"id": "p_brands", "questions": [
                 {"id": "q_brands", "type": "checkbox", "required": True, "title": "Which do you use?",
                  "options": copy.deepcopy(BRANDS)}]},
             {"id": "p_rate", "title": "About {{loop:label}} ({{loop:index}} of {{loop:total}})",
              "repeat": {"source": "q_brands"},
              "questions": [
                  {"id": "q_rate", "type": "radio", "required": True, "title": "Rate {{loop:label}}",
                   "options": [{"value": "1", "label": "Bad"}, {"value": "5", "label": "Great"}]},
                  {"id": "q_why", "type": "text", "title": "Why do you rate {{loop:label}} that way?",
                   "display_logic": {"ref": {"kind": "loop", "id": "value"},
                                     "operator": "is_one_of", "value": ["netflix", "max"]}}]},
             # follow-up only for the items rated 5 on the earlier repeat: `[@]` binds to this item
             {"id": "p_love", "repeat": {"source": "q_brands"}, "questions": [
                 {"id": "q_love", "type": "text", "title": "What do you love about {{loop:label}}?",
                  "display_logic": {"ref": {"kind": "question", "id": "q_rate[@]"},
                                    "operator": "equals", "value": "5"}}]},
             {"id": "p_end", "questions": [
                 {"id": "q_age", "type": "number", "title": "Age?"}]}],
         "actions": [{"type": "complete", "redirect": {"kind": "default"}}]}
    s.update(over)
    return {"survey": s}


def _start(spec, uid="1"):
    rt = SurveyRuntime(spec)
    s = rt.new_session(f"sess:{uid}", {"uid": uid, "rid": "r"})
    return rt, s, rt.start(s, rt.initial_quota_state())


# ------------------------------------------------------------------ page repeating
def test_page_repeats_once_per_selected_item_in_source_order():
    rt, s, step = _start(_survey())
    step = rt.submit(s, "p_brands", {"q_brands": ["max", "netflix"]})
    page = step["page"]
    assert page["page_id"] == "p_rate[netflix]"                 # source order, not answer order
    assert page["title"] == "About Netflix (1 of 2)"
    assert page["iteration"] == {"value": "netflix", "label": "Netflix", "index": 1, "total": 2}
    assert [q["id"] for q in page["questions"]] == ["q_rate[netflix]", "q_why[netflix]"]
    assert page["questions"][0]["title"] == "Rate Netflix"

    step = rt.submit(s, "p_rate[netflix]", {"q_rate[netflix]": "1"})
    assert step["page"]["page_id"] == "p_rate[max]"
    assert step["page"]["title"] == "About Max (2 of 2)"
    step = rt.submit(s, "p_rate[max]", {"q_rate[max]": "5"})
    # the follow-up repeat only runs for the item rated 5 (`q_rate[@]`), so netflix is skipped
    assert step["page"]["page_id"] == "p_love[max]"
    assert step["page"]["iteration"]["index"] == 1 and step["page"]["iteration"]["total"] == 1
    assert step["page"]["questions"][0]["title"] == "What do you love about Max?"
    step = rt.submit(s, "p_love[max]", {"q_love[max]": "the shows"})
    assert step["page"]["page_id"] == "p_end"
    assert s["answers"]["q_rate[netflix]"] == "1" and s["answers"]["q_rate[max]"] == "5"
    assert "loop" not in s


def test_exclusive_option_is_never_repeated_and_empty_loop_skips_the_page():
    rt, s, step = _start(_survey())
    step = rt.submit(s, "p_brands", {"q_brands": ["none"]})
    assert step["page"]["page_id"] == "p_end"


def test_stale_iteration_submit_rerenders_current_without_storing():
    rt, s, step = _start(_survey())
    rt.submit(s, "p_brands", {"q_brands": ["netflix", "hulu"]})
    rt.submit(s, "p_rate[netflix]", {"q_rate[netflix]": "1"})
    # Browser Back: re-posting the netflix iteration while on hulu changes nothing
    step = rt.submit(s, "p_rate[netflix]", {"q_rate[netflix]": "5"})
    assert step["page"]["page_id"] == "p_rate[hulu]"
    assert s["answers"]["q_rate[netflix]"] == "1"
    # A bare page id (no item) for a repeated page is stale too
    step = rt.submit(s, "p_rate", {"q_rate": "5"})
    assert step["page"]["page_id"] == "p_rate[hulu]" and "q_rate" not in s["answers"]


def test_required_is_enforced_per_iteration():
    rt, s, step = _start(_survey())
    rt.submit(s, "p_brands", {"q_brands": ["hulu"]})
    step = rt.submit(s, "p_rate[hulu]", {})
    assert step["ok"] is False and "q_rate[hulu]" in step["errors"]


def test_resume_rerenders_the_same_iteration():
    rt, s, _ = _start(_survey())
    rt.submit(s, "p_brands", {"q_brands": ["netflix", "hulu"]})
    rt.submit(s, "p_rate[netflix]", {"q_rate[netflix]": "1"})
    assert rt.current_page(s)["page"]["page_id"] == "p_rate[hulu]"


def test_not_selected_mode_and_exclude():
    spec = _survey()
    spec["survey"]["pages"][1]["repeat"] = {"source": "q_brands", "mode": "not_selected", "exclude": ["max"]}
    rt, s, _ = _start(spec)
    step = rt.submit(s, "p_brands", {"q_brands": ["netflix"]})
    assert step["page"]["page_id"] == "p_rate[hulu]"           # max excluded, none is exclusive
    step = rt.submit(s, "p_rate[hulu]", {"q_rate[hulu]": "1"})
    assert step["page"]["page_id"] == "p_end"


def test_repeat_by_value_and_max_cap():
    spec = {"survey": {"id": "cars", "version": 1, "title": "Cars", "pages": [
        {"id": "p1", "questions": [{"id": "q_n", "type": "number", "title": "How many cars?"}]},
        {"id": "p_car", "repeat": {"source": "q_n", "by": "value", "max": 3}, "questions": [
            {"id": "q_make", "type": "text", "title": "Car {{loop:index}}: make?"}]}],
        "actions": [{"type": "complete"}]}}
    assert validate(spec) == []
    rt, s, _ = _start(spec)
    step = rt.submit(s, "p1", {"q_n": "7"})
    seen = []
    while "page" in step:
        seen.append(step["page"]["page_id"])
        assert step["page"]["questions"][0]["title"] == f"Car {len(seen)}: make?"
        step = rt.submit(s, step["page"]["page_id"], {step["page"]["questions"][0]["id"]: "x"})
    assert seen == ["p_car[1]", "p_car[2]", "p_car[3]"]        # capped at max


def test_randomize_is_stable_per_session():
    spec = _survey()
    spec["survey"]["pages"][1]["repeat"]["randomize"] = True

    def order(uid):
        rt, s, _ = _start(spec, uid)
        step = rt.submit(s, "p_brands", {"q_brands": ["netflix", "hulu", "max"]})
        out = []
        while step["page"]["page_id"] != "p_end":
            pid = step["page"]["page_id"]
            out.append(pid)
            step = rt.submit(s, pid, {q["id"]: "1" for q in step["page"]["questions"]})
        return out

    assert order("a") == order("a")
    assert any(order(str(i)) != order("a") for i in range(20))


def test_disqualify_fires_mid_loop_but_skip_waits_for_the_loop_to_end():
    spec = _survey(actions=[
        {"type": "disqualify", "reason_code": "hates_hulu",
         "when": {"ref": {"kind": "question", "id": "q_rate[hulu]"}, "operator": "equals", "value": "1"}},
        {"type": "skip", "to_page": "p_end",
         "when": {"ref": {"kind": "question", "id": "q_rate[netflix]"}, "operator": "equals", "value": "5"}},
        {"type": "complete"}])
    assert validate(spec) == []
    rt, s, _ = _start(spec)
    rt.submit(s, "p_brands", {"q_brands": ["netflix", "hulu", "max"]})
    step = rt.submit(s, "p_rate[netflix]", {"q_rate[netflix]": "5", "q_why[netflix]": "great"})
    assert step["page"]["page_id"] == "p_rate[hulu]"           # skip didn't cut the loop short
    step = rt.submit(s, "p_rate[hulu]", {"q_rate[hulu]": "1"})
    assert step["terminal"]["reason_code"] == "hates_hulu"     # but a screen-out ends it


# ------------------------------------------------------------- question repeating
def _qrep():
    return {"survey": {"id": "qrep", "version": 1, "title": "Q repeat", "pages": [
        {"id": "p1", "questions": [{"id": "q_brands", "type": "checkbox", "title": "Use?",
                                    "options": copy.deepcopy(BRANDS)}]},
        {"id": "p2", "questions": [
            {"id": "q_intro", "type": "instructions", "title": "A few quick ratings"},
            {"id": "q_sat", "type": "rating", "required": True, "title": "How satisfied with {{loop:label}}?",
             "scale_points": [1, 2, 3, 4, 5], "repeat": {"source": "q_brands"}},
            {"id": "q_hulu_only", "type": "text", "title": "Hulu note",
             "repeat": {"source": "q_brands"},
             "display_logic": {"ref": {"kind": "loop", "id": "value"}, "operator": "equals", "value": "hulu"}}]}],
        "actions": [{"type": "complete"}]}}


def test_question_repeats_on_one_page_with_per_instance_logic():
    spec = _qrep()
    assert validate(spec) == []
    rt, s, _ = _start(spec)
    step = rt.submit(s, "p1", {"q_brands": ["netflix", "hulu"]})
    page = step["page"]
    assert page["page_id"] == "p2" and "iteration" not in page
    assert [q["id"] for q in page["questions"]] == ["q_intro", "q_sat[netflix]", "q_sat[hulu]", "q_hulu_only[hulu]"]
    assert page["questions"][2]["title"] == "How satisfied with Hulu?"
    assert page["questions"][2]["repeat_item"]["label"] == "Hulu"

    step = rt.submit(s, "p2", {"q_sat[netflix]": "4"})
    assert step["ok"] is False and list(step["errors"]) == ["q_sat[hulu]"]
    step = rt.submit(s, "p2", {"q_sat[netflix]": "4", "q_sat[hulu]": "2", "q_sat": "9", "q_sat[max]": "9"})
    assert "terminal" in step
    assert {k: v for k, v in s["answers"].items() if k.startswith("q_sat")} == \
        {"q_sat[netflix]": "4", "q_sat[hulu]": "2"}               # only rendered instances are stored


# ------------------------------------------------------------------ export + reports
def test_export_has_one_entry_per_item_with_its_label():
    rt, s, _ = _start(_survey())
    rt.submit(s, "p_brands", {"q_brands": ["netflix", "hulu"]})
    rt.submit(s, "p_rate[netflix]", {"q_rate[netflix]": "5", "q_why[netflix]": "shows"})
    rt.submit(s, "p_rate[hulu]", {"q_rate[hulu]": "1"})
    rt.submit(s, "p_love[netflix]", {"q_love[netflix]": "originals"})
    rt.submit(s, "p_end", {"q_age": "30"})
    resp = response_from_session(rt, s)
    e = resp["survey_data"]["q_rate[netflix]"]
    assert e["question"] == "Rate Netflix" and e["answer"] == "Great"
    assert e["repeat"] == {"source": "q_brands", "value": "netflix", "label": "Netflix"}
    assert resp["survey_data"]["q_why[netflix]"]["question"] == "Why do you rate Netflix that way?"
    cols, rows = flatten(rt, [resp])
    assert cols.index("q_rate[netflix]") < cols.index("q_rate[hulu]") < cols.index("q_age")
    assert rows[0]["q_rate[hulu]"] == "Bad"


def test_simulate_covers_repeats():
    cov = coverage(_survey(), n=60, seed=3)
    assert cov["statuses"].get("completed", 0) == 60 and cov["problems"] == []


# ------------------------------------------------------------------------ validation
def _errs(spec):
    return "\n".join(validate(spec))


def test_valid_repeat_survey_passes():
    assert validate(_survey()) == []


def test_validator_catches_repeat_mistakes():
    bad = _survey()
    bad["survey"]["pages"][1]["repeat"] = {"source": "q_age"}             # later page, wrong type
    e = _errs(bad)
    assert "must be on an earlier page" in e and "choice question" in e

    bad = _survey()
    bad["survey"]["pages"][1]["repeat"]["exclude"] = ["disney"]
    assert "repeat.exclude 'disney' is not an option" in _errs(bad)

    bad = _survey()
    bad["survey"]["pages"][0]["questions"][0]["options"][0]["value"] = "net flix"
    assert "can't key per-item answers" in _errs(bad)

    bad = _qrep()
    bad["survey"]["pages"][1]["repeat"] = {"source": "q_brands"}           # question repeat on repeated page
    assert "can't repeat a question on a repeated page" in _errs(bad)


def test_validator_catches_bad_references_to_repeated_answers():
    def with_action(when):
        s = _survey(actions=[{"type": "disqualify", "reason_code": "x", "when": when}, {"type": "complete"}])
        return _errs(s)

    ref = lambda rid: {"ref": {"kind": "question", "id": rid}, "operator": "equals", "value": "1"}
    assert "without an item" in with_action(ref("q_rate"))
    assert "'disney' is not an item" in with_action(ref("q_rate[disney]"))
    assert "only works inside a repeat" in with_action(ref("q_rate[@]"))
    assert "is not repeated" in with_action(ref("q_age[1]"))
    assert with_action(ref("q_rate[hulu]")) == ""
    assert "only works inside a repeat" in with_action(
        {"ref": {"kind": "loop", "id": "value"}, "operator": "equals", "value": "hulu"})

    s = _survey()
    s["survey"]["pages"][3]["questions"][0]["title"] = "Thanks for rating {{loop:label}}"
    assert "only meaningful inside a repeat" in _errs(s)
    s["survey"]["pages"][3]["questions"][0]["title"] = "You rated Hulu {{q:q_rate[hulu].answer|—}}"
    assert _errs(s) == ""
    s["survey"]["pages"][3]["questions"][0]["title"] = "You rated Hulu {{q:q_rate[hulu].answer}}"
    assert "wasn't asked about 'hulu'" in _errs(s)                  # warns: hulu may not have been picked


def test_validator_rejects_logic_on_an_answer_from_the_same_page():
    # the classic mistake: a follow-up on the same (repeated) page as the answer it depends on —
    # the page is built before that answer exists, so it would never show
    s = _survey()
    s["survey"]["pages"][1]["questions"][1]["display_logic"] = {
        "ref": {"kind": "question", "id": "q_rate[@]"}, "operator": "equals", "value": "5"}
    assert "which is on this page" in _errs(s)


# ----------------------------------------------------------------------- HTTP runtime
def test_http_runtime_walks_a_repeated_page():
    from test_server_http import _get, _post, _tok, running_server
    with running_server(_survey()) as base:
        _, html = _get(f"{base}/s/rep?uid=1&rid=2")
        sid = re.search(r'name=session_id value="([^"]+)"', html).group(1)
        tok = _tok(html)
        _, html = _post(f"{base}/s/rep/submit", [("session_id", sid), ("session_token", tok), ("page_id", "p_brands"),
                                                   ("q_brands", "netflix"), ("q_brands", "hulu")])
        assert "About Netflix (1 of 2)" in html and 'name="q_rate[netflix]"' in html
        pid = re.search(r'name=page_id value="([^"]+)"', html).group(1)
        assert pid == "p_rate[netflix]"
        _, html = _post(f"{base}/s/rep/submit", {"session_id": sid, "session_token": tok, "page_id": pid,
                                                 "q_rate[netflix]": "5"})
        assert "About Hulu (2 of 2)" in html


# ------------------------------------------------------------------- bundled example
def test_bundled_example_walks_every_repeat_form():
    import json
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "surveys", "streaming-brand-ratings.survey.json")
    spec = json.load(open(path))
    assert validate(spec) == []
    assert coverage(spec, n=200, seed=5)["problems"] == []
    rt, s, step = _start(spec)
    script = [
        ("p_services", {"q_services": ["netflix", "max", "hulu"]}),
        ("p_rate[netflix]", {"q_sat[netflix]": "5", "q_worth[netflix]": "yes"}),
        ("p_rate[max]", {"q_sat[max]": "2", "q_worth[max]": "no"}),
        ("p_rate[hulu]", {"q_sat[hulu]": "4", "q_worth[hulu]": "unsure"}),
        ("p_love[netflix]", {"q_love[netflix]": "originals"}),        # only items rated >= 4
        ("p_love[hulu]", {"q_love[hulu]": "next-day TV"}),
        ("p_cancel", {"q_cancel[netflix]": "unlikely", "q_cancel[max]": "likely",
                      "q_cancel[hulu]": "maybe", "q_cancel_why[max]": "a price cut"}),
        ("p_household", {"q_people": "2"}),
        ("p_person[1]", {"q_person_age[1]": "13_17"}),
        ("p_person[2]", {"q_person_age[2]": "35_plus"}),
        ("p_end", {}),
    ]
    for pid, answers in script:
        assert step["page"]["page_id"] == pid
        if pid == "p_cancel":                                         # keep-question only where not worth it
            assert [q["id"] for q in step["page"]["questions"]][-1] == "q_cancel_why[max]"
        if pid == "p_end":
            assert step["page"]["questions"][0]["title"].startswith("You rated Netflix 5 out of 5")
        step = rt.submit(s, pid, answers)
    assert step["terminal"]["status"] == "completed"
