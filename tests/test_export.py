import csv
import io
import json
import os

import pytest

from survey_agent.engine.runtime import SurveyRuntime
from survey_agent import exporting

SURVEY = os.path.join(os.path.dirname(__file__), "..", "surveys", "streaming-usage.survey.json")


@pytest.fixture
def rt():
    with open(SURVEY) as f:
        return SurveyRuntime(json.load(f))


def test_flatten_columns_and_rows(rt):
    responses = exporting.collect_responses(rt, simulate_n=50, seed=2)
    cols, rows = exporting.flatten(rt, responses)
    assert len(rows) == 50
    # core + identity + a question + a derived field, no display-only question column
    for c in ("id", "status", "url.uid", "url.rid", "q_service_use", "derived.stream_score"):
        assert c in cols
    assert "q_intro_video" not in cols


def test_to_csv_parses_back(rt):
    responses = exporting.collect_responses(rt, simulate_n=30, seed=4)
    cols, rows = exporting.flatten(rt, responses)
    text = exporting.to_csv(cols, rows)
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert len(parsed) == 30
    assert set(cols) == set(parsed[0].keys())
    # multi-select checkbox is pipe-joined when present
    assert all("," not in (r.get("q_services") or "") for r in parsed)


def test_piped_option_answer_exports_label_not_raw_value():
    """A piped-option (`options_source`) question has no static `options` of its own; it inherits the
    value->label map of its `pipe_from` source, so its answer must export as the human label, not the
    raw stored value. Regression for the standing export-fidelity gap (DESIGN_DECISIONS 'genuinely open').
    The same label_lut also drives piping tokens, so a `{{q:…}}` over the piped chain resolves to labels."""
    from survey_agent.engine import export
    spec = {
        "id": "piptest", "version": 1, "title": "pipe test",
        "pages": [
            {"id": "p1", "questions": [{"id": "q_brands", "type": "checkbox", "title": "Which brands?",
                "options": [{"value": "nflx", "label": "Netflix"},
                            {"value": "hbo", "label": "HBO Max"},
                            {"value": "dis", "label": "Disney+"}]}]},
            {"id": "p2", "questions": [{"id": "q_fav", "type": "radio",
                "title": "Favorite of {{q:q_brands.answer}}?",
                "options_source": {"pipe_from": "q_brands", "mode": "selected"}}]},
        ],
    }
    rt = SurveyRuntime(spec)
    s = rt.new_session("sess1", {"uid": "u1", "rid": "1"})
    rt.start(s)
    assert rt.submit(s, "p1", {"q_brands": ["nflx", "dis"]})["ok"]
    assert rt.submit(s, "p2", {"q_fav": "dis"})["ok"]
    resp = export.response_from_session(rt, s)
    # the piped-option answer exports the inherited label, NOT the raw value "dis"
    assert resp["survey_data"]["q_fav"]["answer"] == "Disney+"
    # and a piping token over the piped source resolves to labels too
    assert resp["survey_data"]["q_fav"]["question"] == "Favorite of Netflix, Disney+?"
