"""Audience-driven persona testing — declare a population (weighted segments / traits), generate a
COHERENT synthetic sample, and a `persona_source` seam for real-data-grounded personas. Offline."""
import json
import os

import pytest

import survey_agent.ai as ai
from survey_agent import cli, simulate
from survey_agent import personas as personas_mod
from survey_agent.personas import SyntheticPersonaSource, get_persona_source

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "src", "survey_agent", "examples", "example.survey.json")


# ----------------------------------------------------------------- audience parsing + sampling
def test_segments_from_various_shapes():
    seg = personas_mod._segments({"segments": [{"weight": 2, "traits": {"a": 1}}, {"weight": 1, "traits": {"b": 2}}]})
    assert [w for w, _, _ in seg] == [2.0, 1.0]
    assert personas_mod._segments({"traits": {"x": 1}})[0][1] == {"x": 1}
    assert personas_mod._segments(["a busy parent"])[0][2] == "a busy parent"
    assert personas_mod._segments(None) == []


def test_synthetic_source_allocates_proportionally():
    aud = {"n": 10, "segments": [
        {"weight": 0.5, "traits": {"usage": "never"}, "label": "never"},
        {"weight": 0.5, "traits": {"usage": "current"}, "label": "current"}]}
    ps = SyntheticPersonaSource().generate({"survey": {}}, aud, n=10)
    assert len(ps) == 10
    by = {}
    for p in ps:
        by[p["segment"]] = by.get(p["segment"], 0) + 1
        assert "traits" in p
    assert by == {"never": 5, "current": 5}                 # deterministic 50/50 allocation


# ----------------------------------------------------------------- the persona_source seam
def test_get_persona_source_builtin_and_import_path():
    assert isinstance(get_persona_source(), SyntheticPersonaSource)
    assert isinstance(get_persona_source("synthetic"), SyntheticPersonaSource)
    # bring-your-own via an import path resolves a class
    src = get_persona_source("survey_agent.personas:SyntheticPersonaSource")
    assert isinstance(src, SyntheticPersonaSource)


def test_get_persona_source_unknown_raises():
    with pytest.raises(ValueError):
        get_persona_source("nope")


# ----------------------------------------------------------------- coherent stub answering
def test_stub_respond_honors_a_declared_trait():
    q = {"id": "q_use", "type": "radio", "title": "How often do you use it?",
         "options": [{"value": "current", "label": "Currently"}, {"value": "never", "label": "Never"}]}
    assert ai.StubLLM().respond({"label": "x", "traits": {"usage": "never"}}, q) == "never"
    assert ai.StubLLM().respond({"label": "x", "traits": {"usage": "current"}}, q) == "current"


def test_stub_respond_complies_with_an_attention_instruction():
    # the prompt names the answer; most-specific label wins ("strongly agree", not "agree")
    q = {"id": "q_att", "type": "radio", "title": "For quality control, please select 'Strongly agree'.",
         "options": [{"value": "a", "label": "Agree"}, {"value": "sa", "label": "Strongly agree"}]}
    assert ai.StubLLM().respond({"label": "anyone", "traits": {}}, q) == "sa"


# ----------------------------------------------------------------- end to end against a declared population
def test_pretest_against_a_declared_audience(monkeypatch):
    monkeypatch.setenv("SURVEY_LLM", "stub")
    spec = json.load(open(EXAMPLE))
    aud = {"n": 8, "segments": [
        {"weight": 0.5, "traits": {"consent": "yes", "usage": "never"}, "label": "never"},
        {"weight": 0.5, "traits": {"consent": "yes", "usage": "current", "age": 30}, "label": "current"}]}
    res = simulate.persona_pretest(spec, audience=aud)
    assert res["source"] == "synthetic" and res["audience"] is True and res["n"] == 8
    # coherent: 'never' segment screens out as non_user; 'current' segment qualifies
    assert res["reasons"].get("non_user", 0) >= 4
    assert res["statuses"].get("completed", 0) >= 4
    assert all(r.get("segment") in ("never", "current") for r in res["results"])


def test_explicit_answers_win_over_traits(monkeypatch):
    # the exact-control path: persona.answers keyed by question id is used verbatim (engine validates),
    # regardless of traits. Here q_service_use="never" -> the non_user screen-out fires.
    monkeypatch.setenv("SURVEY_LLM", "stub")
    spec = json.load(open(EXAMPLE))
    persona = {"label": "exact", "traits": {"usage": "current"},
               "answers": {"q_consent": "yes", "q_service_use": "never"}}
    res = simulate.persona_pretest(spec, personas=[persona])
    assert res["results"][0]["status"] == "disqualified"
    assert res["results"][0]["reason_code"] == "non_user"


def test_raw_personas_carries_answers():
    aud = {"segments": [{"weight": 1, "traits": {"a": 1}, "answers": {"q1": "x"}, "label": "s"}]}
    raw = personas_mod._raw_personas(aud)
    assert raw[0][1]["answers"] == {"q1": "x"}
    ps = SyntheticPersonaSource().generate({"survey": {}}, aud, n=2)
    assert all(p["answers"] == {"q1": "x"} for p in ps)


def test_validate_audience_accepts_good_and_flags_bad():
    good = {"n": 10, "segments": [{"weight": 1, "traits": {"usage": "never"}, "answers": {"q_age": 30}}]}
    assert personas_mod.validate_audience(good) == []
    # a typo'd persona key is rejected by the schema (additionalProperties:false)
    bad = {"segments": [{"traits": {}, "wieght": 2}]}
    errs = personas_mod.validate_audience(bad)
    assert errs and any("wieght" in e for e in errs)


def test_cli_pretest_with_a_one_off_trait(monkeypatch, capsys):
    monkeypatch.setenv("SURVEY_LLM", "stub")
    rc = cli.main(["pretest", EXAMPLE, "--n", "3", "--trait", "usage=never"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "non_user" in out and "for testing" in out         # screens out + carries the QA disclaimer
