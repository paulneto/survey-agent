"""Methodology critic — offline heuristic review of a survey draft. High-precision rules only
(advisory); a real key gives a fuller read. Sees the definition, never respondent data."""
import json
import os

import survey_agent.ai as ai
from survey_agent import cli
from survey_agent.engine import validate as v

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "src", "survey_agent", "examples", "example.survey.json")


def _bad():
    return {"survey": {
        "id": "bad", "title": "Bad", "settings": {"url_variables": ["uid", "rid"]},
        "pages": [{"id": "p", "questions": [
            {"id": "q_lead", "type": "radio", "title": "Don't you agree our app is great?",
             "options": [{"value": "y", "label": "Yes"}, {"value": "n", "label": "No"}]},
            {"id": "q_db", "type": "radio", "title": "How happy are you with the price and the quality?",
             "options": [{"value": "a", "label": "Happy"}, {"value": "b", "label": "Neutral"},
                         {"value": "c", "label": "Unhappy"}]},
            {"id": "q_age", "type": "radio", "title": "Age band?",
             "options": [{"value": "1", "label": "18-30"}, {"value": "2", "label": "30-40"},
                         {"value": "3", "label": "40-50"}]}]}],
        "actions": [{"type": "complete"}]}}


def _types(c):
    return {i["type"] for i in c["issues"]}


def test_stub_critique_flags_the_planted_problems():
    c = ai.StubLLM().critique(_bad())
    t = _types(c)
    assert "leading" in t                       # "Don't you agree..."
    assert "double_barreled" in t               # "price and the quality"
    assert "overlapping_ranges" in t            # 18-30 / 30-40 / 40-50
    assert "no_opt_out" in t                    # 3-option single-selects with no neutral
    assert c["engine"] == "stub"
    # leading is surfaced first (severity-sorted)
    assert c["issues"][0]["severity"] == "high"


def test_leading_question_has_a_suggestion():
    issue = next(i for i in ai.StubLLM().critique(_bad())["issues"] if i["type"] == "leading")
    assert issue["id"] == "q_lead" and issue["suggestion"]


def test_clean_survey_has_no_issues():
    # the stub-authored consent screener: yes/no consent (2 opts) + a textarea — nothing to flag
    clean = ai.StubLLM().author("a short study", v.load_schema())
    assert ai.StubLLM().critique(clean)["issues"] == []


def test_bundled_example_is_not_noisy():
    # guardrail against false positives on a real, well-formed survey: no HIGH-severity flags
    spec = json.load(open(EXAMPLE))
    highs = [i for i in ai.StubLLM().critique(spec)["issues"] if i["severity"] == "high"]
    assert highs == [], highs


def test_cli_critique(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SURVEY_LLM", "stub")
    f = tmp_path / "bad.survey.json"
    f.write_text(json.dumps(_bad()))
    rc = cli.main(["critique", str(f)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Methodology review" in out and "leading" in out
