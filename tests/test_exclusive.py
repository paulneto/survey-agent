"""Exclusive checkbox option ("none of the above"): the engine collapses a checkbox answer to the
exclusive option when it's among the picks, dropping co-selected ones — enforced regardless of client."""
from survey_agent.engine.runtime import SurveyRuntime

SURVEY = {"survey": {
    "id": "x", "title": "X", "settings": {"url_variables": []},
    "pages": [{"id": "p1", "questions": [
        {"id": "q_tools", "type": "checkbox", "required": True, "options": [
            {"value": "twitter", "label": "Twitter"},
            {"value": "tiktok", "label": "TikTok"},
            {"value": "none", "label": "None of the above", "exclusive": True},
        ]},
    ]}],
    "actions": [{"type": "complete", "redirect": {"kind": "default"}}],
}}


def _submit(picks):
    rt = SurveyRuntime(SURVEY)
    qs = rt.initial_quota_state()
    s = rt.new_session("x:1:1", {})
    rt.start(s, qs)
    rt.submit(s, "p1", {"q_tools": picks}, qs)
    return s["answers"]["q_tools"]


def test_exclusive_collapses_when_co_selected():
    assert _submit(["twitter", "none", "tiktok"]) == ["none"]


def test_exclusive_alone_is_unchanged():
    assert _submit(["none"]) == ["none"]


def test_non_exclusive_picks_pass_through():
    assert _submit(["twitter", "tiktok"]) == ["twitter", "tiktok"]


def test_single_value_coerced_to_list_then_checked():
    assert _submit("none") == ["none"]               # single posted value still collapses correctly
    assert _submit("twitter") == ["twitter"]
