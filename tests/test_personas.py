"""Persona simulation: LLM-persona synthetic respondents for a qualitative pre-test. Same contract
as conversational mode — the LLM only role-plays an answer, the engine validates + decides flow.
All offline via the deterministic stub."""
import json

import survey_agent.ai as ai
from survey_agent import cli, simulate
from survey_agent.engine import validate as v


def _spec():
    return ai.StubLLM().author("screen people who stream paid video", v.load_schema())


def test_stub_personas_count():
    ps = ai.StubLLM().personas(_spec(), 5)
    assert len(ps) == 5 and all(isinstance(p, str) and p for p in ps)


def test_stub_respond_returns_a_valid_option():
    q = {"id": "q1", "type": "radio", "title": "Do you use it?",
         "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}]}
    assert ai.StubLLM().respond("a heavy user", q) in ("yes", "no")


def test_stub_respond_is_deterministic_per_persona():
    q = {"id": "q1", "type": "radio", "title": "Pick",
         "options": [{"value": c, "label": c} for c in ("a", "b", "c", "d")]}
    s = ai.StubLLM()
    assert s.respond("persona one", q) == s.respond("persona one", q)        # stable


def test_persona_pretest_runs_offline(monkeypatch):
    monkeypatch.setenv("SURVEY_LLM", "stub")
    res = simulate.persona_pretest(_spec(), n=4)
    assert res["n"] == 4
    assert sum(res["statuses"].values()) == 4
    assert all({"persona", "status", "answers"} <= set(r) for r in res["results"])
    assert 0.0 <= res["incidence"] <= 1.0


def test_persona_answers_keep_engine_authoritative(monkeypatch):
    # a recorded options answer is always a VALID option value — the engine validated it, the LLM
    # didn't get to invent one. (q_consent in the stub survey only accepts yes/no.)
    monkeypatch.setenv("SURVEY_LLM", "stub")
    res = simulate.persona_pretest(_spec(), n=6)
    for r in res["results"]:
        if "q_consent" in r["answers"]:
            assert r["answers"]["q_consent"] in ("yes", "no")


def test_explicit_personas_are_used(monkeypatch):
    monkeypatch.setenv("SURVEY_LLM", "stub")
    res = simulate.persona_pretest(_spec(), personas=["a one-off custom persona"])
    assert res["n"] == 1 and res["results"][0]["persona"] == "a one-off custom persona"


def test_cli_pretest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SURVEY_LLM", "stub")
    f = tmp_path / "s.survey.json"
    f.write_text(json.dumps(_spec()))
    rc = cli.main(["pretest", str(f), "--n", "3"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Persona pre-test" in out and "Incidence" in out
