"""Grid (matrix) and ranking question types, end to end through the engine."""
from survey_agent.engine.runtime import SurveyRuntime
from survey_agent.engine import export


def _rt(question, required=True):
    q = {"id": "q", **question}
    return SurveyRuntime({"survey": {"id": "g", "title": "G", "pages": [{"id": "p", "questions": [q]}],
                                     "actions": [{"type": "complete", "redirect": {"kind": "default"}}]}})


GRID = {
    "type": "grid", "required": True, "title": "Rate each",
    "rows": [{"id": "speed", "label": "Speed"}, {"id": "price", "label": "Price"}],
    "options": [{"value": "1", "label": "Poor"}, {"value": "2", "label": "OK"}, {"value": "3", "label": "Great"}],
}


def test_grid_required_needs_every_row():
    rt = _rt(GRID)
    errs = rt.validate_answers(rt.pages[0], {"q": {"speed": "3"}}, rt.new_session("g:1", {}))
    assert "q" in errs                                   # price row missing
    errs2 = rt.validate_answers(rt.pages[0], {"q": {"speed": "3", "price": "2"}}, rt.new_session("g:1", {}))
    assert "q" not in errs2


def test_grid_stores_and_exports_labels():
    rt = _rt(GRID)
    qs = rt.initial_quota_state()
    s = rt.new_session("g:2", {"uid": "1", "rid": "1"})
    rt.start(s, qs)
    step = rt.submit(s, "p", {"q": {"speed": "3", "price": "1"}}, qs)
    assert step["terminal"]["status"] == "completed"
    assert s["answers"]["q"] == {"speed": "3", "price": "1"}      # dict stored as-is
    sd = export.build_survey_data(rt, s)
    assert sd["q"]["answer"] == {"Speed": "Great", "Price": "Poor"}   # exported with row+col labels


def test_checkbox_grid_stores_lists_and_exports_labels():
    rt = _rt({**GRID, "cell_type": "checkbox"})
    qs = rt.initial_quota_state()
    s = rt.new_session("g:cb", {"uid": "1", "rid": "1"})
    rt.start(s, qs)
    step = rt.submit(s, "p", {"q": {"speed": ["3", "2"], "price": ["1"]}}, qs)
    assert step["terminal"]["status"] == "completed"
    sd = export.build_survey_data(rt, s)
    assert sd["q"]["answer"] == {"Speed": ["Great", "OK"], "Price": ["Poor"]}


def test_dropdown_multi_coerces_and_validates():
    rt = _rt({"type": "dropdown_multi", "required": True, "title": "Pick some",
              "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}, {"value": "c", "label": "C"}]})
    # required + empty -> error
    assert "q" in rt.validate_answers(rt.pages[0], {"q": []}, rt.new_session("g:d0", {}))
    qs = rt.initial_quota_state()
    s = rt.new_session("g:d1", {"uid": "1", "rid": "1"})
    rt.start(s, qs)
    step = rt.submit(s, "p", {"q": ["a", "c"]}, qs)
    assert step["terminal"]["status"] == "completed"
    assert s["answers"]["q"] == ["a", "c"]                 # list semantics
    # a single value is coerced to a list too
    s2 = rt.new_session("g:d2", {"uid": "1", "rid": "1"})
    rt.start(s2, qs)
    rt.submit(s2, "p", {"q": "b"}, qs)
    assert s2["answers"]["q"] == ["b"]


def test_ranking_coerces_to_ordered_list():
    rt = _rt({"type": "ranking", "title": "Rank",
              "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}, {"value": "c", "label": "C"}]})
    qs = rt.initial_quota_state()
    s = rt.new_session("g:3", {"uid": "1", "rid": "1"})
    rt.start(s, qs)
    step = rt.submit(s, "p", {"q": ["c", "a", "b"]}, qs)   # submitted in DOM order
    assert step["terminal"]["status"] == "completed"
    assert s["answers"]["q"] == ["c", "a", "b"]            # ordered list preserved
