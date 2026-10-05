"""Piping failure modes: every way a merge token or piped option list can silently go wrong for a
respondent, and the gate (validate / runtime / simulate) that now catches or prevents each one."""
from survey_agent.engine.runtime import SurveyRuntime
from survey_agent.engine.validate import validate
from survey_agent.simulate import coverage

O = [{"value": "netflix", "label": "Netflix"}, {"value": "hulu", "label": "Hulu"},
     {"value": "none", "label": "None of these", "exclusive": True}]


def _spec(pages, actions=None, url_variables=("uid", "rid", "src"), **extra):
    s = {"id": "pipe", "version": 1, "title": "Piping",
         "settings": {"url_variables": list(url_variables)}, "pages": pages,
         "actions": actions or [{"type": "complete"}]}
    s.update(extra)
    return {"survey": s}


def _errs(spec):
    return [e for e in validate(spec) if not e.startswith("[warn]")]


def _warns(spec):
    return [e for e in validate(spec) if e.startswith("[warn]")]


def _walk(spec, answers_by_page, url_vars=None):
    """Answer page by page; return [(page_id, [rendered question titles], {qid: [option labels]})]."""
    rt = SurveyRuntime(spec)
    s = rt.new_session("t", url_vars or {"uid": "1", "rid": "1"})
    step, seen = rt.start(s), []
    while "page" in step:
        p = step["page"]
        seen.append((p["page_id"], [q["title"] for q in p["questions"]],
                     {q["id"]: [o["label"] for o in q.get("options", [])] for q in p["questions"]}))
        step = rt.submit(s, p["page_id"], answers_by_page.get(p["page_id"], {}))
        assert len(seen) < 20
    return seen, step


FAV = {"id": "q_fav", "type": "radio", "title": "Favourite?", "options": O[:2]}


# --------------------------------------------------------------- 1. piping a later / same page
def test_piping_from_a_later_or_the_same_page_is_an_error():
    later = _spec([{"id": "p1", "questions": [{"id": "q_t", "type": "text", "title": "You said {{q:q_fav.answer}}"}]},
                   {"id": "p2", "questions": [FAV]}])
    assert any("which is on a later page" in e for e in _errs(later))
    same = _spec([{"id": "p1", "questions": [FAV, {"id": "q_t", "type": "text", "title": "Why {{q:q_fav.answer}}?"}]}])
    assert any("which is on this page" in e for e in _errs(same))
    piped_same = _spec([{"id": "p1", "questions": [
        {"id": "q_b", "type": "checkbox", "title": "Use?", "options": O},
        {"id": "q_pick", "type": "radio", "title": "Best?", "options_source": {"pipe_from": "q_b"}}]}])
    assert any("options_source.pipe_from 'q_b' is on this page" in e for e in _errs(piped_same))


# ------------------------------------------------------------- 2. typos the old lint let through
def test_unknown_token_kinds_fields_and_rows_are_errors():
    spec = _spec([{"id": "p1", "questions": [FAV, {"id": "q_g", "type": "grid", "title": "Rate",
                                                   "rows": [{"id": "r1", "label": "Price"}], "options": O[:2]}]},
                  {"id": "p2", "questions": [{"id": "q_t", "type": "text",
                                              "title": "{{q:q_fav.answr}} {{qq:q_fav}} {{q:q_g.row:r9.answer}}"}]}])
    e = "\n".join(_errs(spec))
    assert "unknown field 'answr'" in e
    assert "unknown merge token {{qq:q_fav}}" in e
    assert "has no grid row 'r9'" in e


def test_ctx_tokens_resolve_as_documented():
    spec = _spec([{"id": "p1", "questions": [{"id": "q_t", "type": "text", "title": "You use {{ctx:top_app}} most"}]}],
                 settings={"url_variables": ["uid", "rid"], "enrichments": [{"id": "top_app", "source": "static"}]})
    assert _errs(spec) == []
    rt = SurveyRuntime(spec)
    s = rt.new_session("t", {"uid": "1"})
    s["enrichments"] = {"top_app": "Chess"}
    assert rt.start(s)["page"]["questions"][0]["title"] == "You use Chess most"


# ------------------------------------------------------ 3. answers that may not exist → fallback
def test_fallback_renders_when_the_value_is_blank():
    spec = _spec([{"id": "p1", "questions": [FAV]},
                  {"id": "p2", "questions": [{"id": "q_t", "type": "text",
                                              "title": "Why {{q:q_fav.answer|that one}}? From {{url:src|the web}}."}]}])
    assert _errs(spec) == [] and _warns(spec) == []
    seen, _ = _walk(spec, {"p1": {}})
    assert seen[1][1] == ["Why that one? From the web."]
    seen, _ = _walk(spec, {"p1": {"q_fav": "hulu"}}, {"uid": "1", "src": "email"})
    assert seen[1][1] == ["Why Hulu? From email."]


def test_validate_warns_when_a_piped_answer_can_be_missing():
    optional = _spec([{"id": "p1", "questions": [FAV]},
                      {"id": "p2", "questions": [{"id": "q_t", "type": "text", "title": "Why {{q:q_fav.answer}}?"}]}])
    assert any("is optional and left empty" in w for w in _warns(optional))

    gated_page = _spec([
        {"id": "p1", "questions": [{"id": "q_b", "type": "checkbox", "title": "Use?", "required": True, "options": O}]},
        {"id": "p2", "display_logic": {"ref": {"kind": "question", "id": "q_b"}, "operator": "contains", "value": "netflix"},
         "questions": [{**FAV, "required": True}]},
        {"id": "p3", "questions": [{"id": "q_t", "type": "text", "title": "You picked {{q:q_fav.answer}}"}]}])
    assert any("on a page that isn't always shown" in w for w in _warns(gated_page))

    url = _spec([{"id": "p1", "questions": [{"id": "q_t", "type": "text", "title": "Hi from {{url:src}}"}]}])
    assert any("when the link has no 'src'" in w for w in _warns(url))


def test_simulate_reports_tokens_that_render_blank():
    spec = _spec([{"id": "p1", "questions": [FAV]},
                  {"id": "p2", "questions": [{"id": "q_t", "type": "text", "title": "Why do you love {{q:q_fav.answer}}?"}]}])
    cov = coverage(spec, n=200, seed=1)
    assert cov["blank_piping"] and cov["blank_piping"][0]["token"] == "{{q:q_fav.answer}}"
    assert 0 < cov["blank_piping"][0]["count"] < 200            # only those who skipped q_fav
    assert cov["problems"] == []                                   # a warning, not a gate failure
    assert any(w.startswith("piping:") for w in cov["warnings"])
    fixed = _spec([{"id": "p1", "questions": [FAV]},
                   {"id": "p2", "questions": [{"id": "q_t", "type": "text",
                                               "title": "Why do you love {{q:q_fav.answer|it}}?"}]}])
    assert coverage(fixed, n=200, seed=1)["warnings"] == []


# --------------------------------------------------------------------- 4. option piping
PICK = {"id": "q_pick", "type": "radio", "required": True, "title": "Best of those?",
        "options_source": {"pipe_from": "q_b"}}


def test_none_of_these_is_not_piped_forward():
    spec = _spec([{"id": "p1", "questions": [{"id": "q_b", "type": "checkbox", "title": "Use?", "options": O}]},
                  {"id": "p2", "questions": [PICK]}, {"id": "p3", "questions": [FAV]}])
    seen, _ = _walk(spec, {"p1": {"q_b": ["netflix"]}, "p2": {"q_pick": "netflix"}})
    assert seen[1][2]["q_pick"] == ["Netflix"]
    nsel = _spec([{"id": "p1", "questions": [{"id": "q_b", "type": "checkbox", "title": "Use?", "options": O}]},
                  {"id": "p2", "questions": [{**PICK, "options_source": {"pipe_from": "q_b", "mode": "not_selected"}}]},
                  {"id": "p3", "questions": [FAV]}])
    seen, _ = _walk(nsel, {"p1": {"q_b": ["netflix"]}, "p2": {"q_pick": "hulu"}})
    assert seen[1][2]["q_pick"] == ["Hulu"]                       # not "None of these"


def test_a_piped_question_with_no_options_is_skipped_not_stuck():
    spec = _spec([{"id": "p1", "questions": [{"id": "q_b", "type": "checkbox", "title": "Use?", "options": O}]},
                  {"id": "p2", "questions": [PICK]}, {"id": "p3", "questions": [FAV]}])
    for answer in ({}, {"q_b": ["none"]}):                          # left blank, or "None of these"
        seen, step = _walk(spec, {"p1": answer})
        assert [p for p, _, _ in seen] == ["p1", "p3"]               # p2 had nothing to ask → skipped
        assert step["terminal"]["status"] == "completed"
    assert any("so it has none when 'q_b'" in w for w in _warns(spec))


def test_warning_is_silent_when_a_screen_out_handles_the_empty_case():
    spec = _spec([{"id": "p1", "questions": [{"id": "q_b", "type": "checkbox", "title": "Use?", "required": True,
                                              "options": O}]},
                  {"id": "p2", "questions": [PICK]}],
                 actions=[{"type": "disqualify", "reason_code": "non_user",
                           "when": {"ref": {"kind": "question", "id": "q_b"}, "operator": "contains", "value": "none"}},
                          {"type": "complete"}])
    assert validate(spec) == []


def test_bundled_surveys_are_clean():
    import glob
    import json
    import os
    root = os.path.join(os.path.dirname(__file__), "..")
    paths = glob.glob(os.path.join(root, "surveys", "*.survey.json"))
    assert len(paths) >= 4
    for path in paths:
        spec = json.load(open(path))
        assert validate(spec) == [], path
        assert coverage(spec, n=200, seed=2)["problems"] == [], path


# --------------------------------------------------- regressions from the independent review
def test_infinite_or_nan_numbers_are_rejected_not_crashing_a_by_value_repeat():
    spec = _spec([{"id": "p1", "questions": [{"id": "n", "type": "number", "required": True, "title": "How many?"}]},
                  {"id": "p2", "repeat": {"source": "n", "by": "value"},
                   "questions": [{"id": "a", "type": "text", "title": "#{{loop:index}}"}]}])
    rt = SurveyRuntime(spec)
    s = rt.new_session("t", {})
    rt.start(s)
    for bad in ("inf", "1e400", "nan", "-inf"):
        step = rt.submit(s, "p1", {"n": bad})
        assert step["ok"] is False and "n" in step["errors"]


def test_quota_gated_question_agrees_between_eligibility_render_and_submit():
    spec = _spec([{"id": "p1", "questions": [
        {"id": "b", "type": "text", "title": "Only while open",
         "display_logic": {"ref": {"kind": "quota", "id": "Q"}, "operator": "is_not_full"}}]},
        {"id": "p2", "questions": [FAV]}], quotas=[{"id": "Q", "scope": "survey", "limit": 1}])
    rt = SurveyRuntime(spec)
    full = {"Q": {"count": 1, "limit": 1}}
    s = rt.new_session("t", {})
    assert rt.start(s, full)["page"]["page_id"] == "p2"            # p1 has nothing to show → skipped
    s = rt.new_session("u", {})
    page = rt.start(s, {"Q": {"count": 0, "limit": 1}})["page"]
    assert [q["id"] for q in page["questions"]] == ["b"]


def test_chained_option_piping_follows_the_chain():
    spec = _spec([{"id": "p1", "questions": [{"id": "q1", "type": "checkbox", "required": True, "title": "Use?",
                                              "options": O[:2]}]},
                  {"id": "p2", "questions": [{"id": "q2", "type": "checkbox", "required": True, "title": "Like?",
                                              "options_source": {"pipe_from": "q1"}}]},
                  {"id": "p3", "questions": [{"id": "q3", "type": "radio", "required": True, "title": "Best?",
                                              "options_source": {"pipe_from": "q2"}}]}])
    seen, _ = _walk(spec, {"p1": {"q1": ["netflix", "hulu"]}, "p2": {"q2": ["hulu"]}, "p3": {"q3": "hulu"}})
    assert seen[2][2]["q3"] == ["Hulu"]


def test_current_item_ref_must_share_the_loops_source():
    spec = _spec([
        {"id": "p0", "questions": [{"id": "brands", "type": "checkbox", "required": True, "title": "B?", "options": O[:2]},
                                   {"id": "sports", "type": "checkbox", "required": True, "title": "S?",
                                    "options": [{"value": "golf", "label": "Golf"}]}]},
        {"id": "p1", "repeat": {"source": "brands"}, "questions": [
            {"id": "aware", "type": "radio", "required": True, "title": "Aware of {{loop:label}}?",
             "options": [{"value": "y", "label": "Yes"}, {"value": "n", "label": "No"}]}]},
        {"id": "p2", "repeat": {"source": "sports"}, "questions": [
            {"id": "x", "type": "text", "title": "{{loop:label}}",
             "display_logic": {"ref": {"kind": "question", "id": "aware[@]"}, "operator": "equals", "value": "y"}}]}])
    assert any("same source and mode" in e for e in _errs(spec))
    # same source, but one repeats over what was picked and the other over what wasn't
    spec["survey"]["pages"][2]["repeat"] = {"source": "brands", "mode": "not_selected"}
    assert any("same source and mode" in e for e in _errs(spec))


def test_braces_inside_a_token_are_rejected():
    for title in ("Hi {{q:q_fav|a}b}}", "Hi {{q:q_fav|{{url:uid}}}}", "Hi {{q:q_fav"):
        spec = _spec([{"id": "p1", "questions": [FAV]},
                      {"id": "p2", "questions": [{"id": "t", "type": "text", "title": title}]}])
        assert any("malformed merge token" in e for e in _errs(spec)), title


def test_piped_respondent_text_cannot_become_a_link_or_image():
    spec = _spec([{"id": "p1", "questions": [{"id": "name", "type": "text", "required": True, "title": "Name?"}]},
                  {"id": "p2", "title": "Hi {{q:name.answer}}", "questions": [
                      {"id": "t", "type": "text", "title": "Thanks {{q:name.answer}} from {{url:src|x}}"}]}])
    from survey_agent.render import md_inline
    rt = SurveyRuntime(spec)
    s = rt.new_session("t", {"src": "![](https://evil.example/p.png)"})
    rt.start(s)
    page = rt.submit(s, "p1", {"name": "[Claim $500](https://evil.example/x)"})["page"]
    for item in (page, page["questions"][0]):
        html = md_inline(item.get("title_md") or item["title"])   # what the HTML runtime renders
        assert "<a " not in html and "<img" not in html
        assert "Claim $500" in html and "evil.example" not in html and "[" not in html
        assert "\\" not in item["title"]                           # plain copy (chat, JSON) is clean
    assert page["title"] == "Hi Claim $500"
    # ...but the stored values are exactly what arrived
    assert s["answers"]["name"] == "[Claim $500](https://evil.example/x)"


def test_warn_heuristics_are_not_noisy():
    # a required answer on a page no skip can jump past: no warning
    spec = _spec([{"id": "p0", "questions": [{**FAV, "required": True}]},
                  {"id": "p1", "questions": [{"id": "g", "type": "radio", "required": True, "title": "Go?",
                                              "options": [{"value": "y", "label": "Y"}]}]},
                  {"id": "p2", "questions": [{"id": "x", "type": "text", "title": "x"}]},
                  {"id": "p3", "questions": [{"id": "t", "type": "text", "title": "You like {{q:q_fav.answer}}"}]}],
                 actions=[{"type": "skip", "to_page": "p3",
                           "when": {"ref": {"kind": "question", "id": "g"}, "operator": "equals", "value": "y"}},
                          {"type": "complete"}])
    assert _warns(spec) == []
    # the consuming question is itself gated on the answer existing: no warning
    guarded = _spec([{"id": "p1", "questions": [FAV]},
                     {"id": "p2", "questions": [{"id": "t", "type": "text", "title": "Why {{q:q_fav.answer}}?",
                                                 "display_logic": {"ref": {"kind": "question", "id": "q_fav"},
                                                                   "operator": "is_answered"}}]}])
    assert _warns(guarded) == []
    # a screen-out that ANDs the source with something else doesn't count as handling it
    anded = _spec([{"id": "p1", "questions": [{"id": "q_b", "type": "checkbox", "title": "Use?", "options": O}]},
                   {"id": "p2", "questions": [PICK]}],
                  actions=[{"type": "disqualify", "reason_code": "r", "when": {"op": "and", "conditions": [
                      {"ref": {"kind": "question", "id": "q_b"}, "operator": "contains", "value": "none"},
                      {"ref": {"kind": "url_var", "id": "src"}, "operator": "equals", "value": "zzz"}]}},
                      {"type": "complete"}])
    assert any("so it has none when 'q_b'" in w for w in _warns(anded))


def test_option_and_row_labels_cannot_inject_links_in_the_html_runtime():
    from test_server_http import _get, running_server
    spec = _spec([{"id": "p1", "questions": [
        {"id": "c", "type": "radio", "title": "Pick", "options": [{"value": "a", "label": "Hi {{url:src|there}}"}]},
        {"id": "g", "type": "grid", "title": "Rate", "rows": [{"id": "r", "label": "Row {{url:src|x}}"}],
         "options": [{"value": "1", "label": "One"}]}]}])
    import urllib.parse
    with running_server(spec, SURVEY_PREVIEW_TOKEN="t") as base:
        q = urllib.parse.urlencode({"uid": "1", "rid": "1", "preview": "t",
                                    "src": "[Claim $500](https://evil.example/x)"})
        _, html = _get(f"{base}/s/pipe?{q}")
    assert "evil.example" not in html and "[Claim" not in html      # shown as its words only
    assert "Hi Claim $500" in html and "Row Claim $500" in html


def test_nul_bytes_in_a_piped_value_dont_crash_rendering():
    from survey_agent.render import md_inline
    assert md_inline("a\x000\x00b") == "a0b"
    spec = _spec([{"id": "p1", "title": "Hi {{url:src|x}}", "questions": [FAV]}])
    rt = SurveyRuntime(spec)
    s = rt.new_session("t", {"src": "x\x000\x00y"})
    page = rt.start(s)["page"]
    md_inline(page.get("title_md") or page["title"])


def test_authored_label_markdown_survives_piping():
    spec = _spec([{"id": "p1", "questions": [{"id": "q_b", "type": "radio", "required": True, "title": "Pick",
                                              "options": [{"value": "dp", "label": "**Dr Pepper**"}]}]},
                  {"id": "p2", "title": "About {{q:q_b.answer}}", "questions": [FAV]}])
    seen, _ = _walk(spec, {"p1": {"q_b": "dp"}})
    rt = SurveyRuntime(spec)
    s = rt.new_session("t", {})
    rt.start(s)
    page = rt.submit(s, "p1", {"q_b": "dp"})["page"]
    assert page["title"] == "About **Dr Pepper**" and "title_md" not in page   # authored → not escaped


def test_brace_check_ignores_json_and_object_payloads():
    spec = _spec([{"id": "p1", "questions": [{"id": "i", "type": "instructions", "title": "Example",
                                              "body": 'Send `{"a": {"b": 1}}` to the API'}]}],
                 actions=[{"type": "webhook", "url": "https://h.example/x",
                           "when": {"ref": {"kind": "url_var", "id": "uid"}, "operator": "is_answered"},
                           "payload": {"meta": {"src": {"k": "v"}}, "uid": "{{url:uid}}"}},
                          {"type": "complete"}])
    assert _errs(spec) == []


def test_guards_that_dont_prove_an_answer_still_warn():
    def with_guard(dl):
        return _spec([{"id": "p1", "questions": [FAV, {"id": "z", "type": "radio", "title": "Z?",
                                                       "options": [{"value": "y", "label": "Y"}]}]},
                      {"id": "p2", "questions": [{"id": "t", "type": "text", "title": "Why {{q:q_fav.answer}}?",
                                                  "display_logic": dl}]}])
    ans = {"ref": {"kind": "question", "id": "q_fav"}, "operator": "is_answered"}
    assert not any("q_fav" in w for w in _warns(with_guard(ans)))
    blank = {"ref": {"kind": "question", "id": "q_fav"}, "operator": "is_not_answered"}
    assert any("q_fav" in w for w in _warns(with_guard(blank)))
    either = {"op": "or", "conditions": [ans, {"ref": {"kind": "question", "id": "z"}, "operator": "equals",
                                              "value": "y"}]}
    assert any("q_fav" in w for w in _warns(with_guard(either)))


def test_malformed_repeat_is_a_schema_error_not_a_crash():
    for bad in (True, "brands", 3):
        spec = _spec([{"id": "p1", "questions": [FAV]},
                      {"id": "p2", "repeat": bad, "questions": [{"id": "t", "type": "text", "title": "x"}]}])
        errs = validate(spec)
        assert any("repeat" in e for e in errs)
