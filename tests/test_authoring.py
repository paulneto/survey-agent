"""Tests for the agentic authoring surface (survey-agent author / schema), the LLM provider seam
(Claude or Codex, offline stub fallback), and the bring-your-own-store seam."""
import json
import os

import pytest

from survey_agent import ai, cli
import survey_agent.store as store_pkg
from survey_agent.engine import validate as v


# ----------------------------------------------------------------- stub authoring
def test_stub_author_emits_valid_survey():
    spec = ai.StubLLM().author("screen coffee drinkers who buy beans online", v.load_schema())
    assert "survey" in spec
    errs = [e for e in v.validate(spec) if not e.startswith("[warn]")]
    assert errs == [], errs


def test_stub_authored_survey_has_both_terminals():
    # the coverage gate needs a reachable complete AND disqualify
    spec = ai.StubLLM().author("anything", v.load_schema())
    kinds = {a["type"] for a in spec["survey"]["actions"]}
    assert "complete" in kinds and "disqualify" in kinds


def test_stub_author_slug_is_derived_from_brief():
    spec = ai.StubLLM().author("Streaming Habits Q3", v.load_schema())
    assert spec["survey"]["id"] == "streaming-habits-q3"


# ----------------------------------------------------------------- provider selection
def test_get_llm_defaults_to_stub_without_keys(monkeypatch):
    for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SURVEY_LLM"):
        monkeypatch.delenv(k, raising=False)
    assert ai.get_llm().name == "stub"


def test_survey_llm_stub_forces_stub_even_with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("SURVEY_LLM", "stub")
    assert ai.get_llm().name == "stub"
    assert not ai.has_real_llm()


def test_provider_falls_back_to_stub_when_sdk_missing(monkeypatch):
    # a key is present but the provider SDK may not be installed in the test env: get_llm must not
    # raise — it falls back to the stub.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("SURVEY_LLM", "auto")
    assert ai.get_llm().name in ("anthropic", "stub")   # anthropic only if the SDK is installed


# ----------------------------------------------------------------- prompt + json extraction
def test_author_prompt_includes_brief_and_repair_errors():
    p = ai._author_prompt("my brief", {"x": 1}, prior={"survey": {}}, errors=["bad ref 'q1'"])
    assert "my brief" in p and "bad ref 'q1'" in p and "FAILED validation" in p


def test_extract_json_tolerates_fences_and_prose():
    assert ai._extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert ai._extract_json('Sure! Here it is: {"a": [1,2]} — done.') == {"a": [1, 2]}
    with pytest.raises(ValueError):
        ai._extract_json("no json here")


# ----------------------------------------------------------------- CLI: author + schema
def test_cli_author_writes_validated_survey(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SURVEY_LLM", "stub")
    out = tmp_path / "surveys"
    rc = cli.main(["author", "screen pet owners", "--out", str(out), "--simulate", "0"])
    assert rc == 0
    files = list(out.glob("*.survey.json"))
    assert len(files) == 1
    spec = json.loads(files[0].read_text())
    assert [e for e in v.validate(spec) if not e.startswith("[warn]")] == []


def test_cli_author_simulate_runs_coverage_gate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SURVEY_LLM", "stub")
    rc = cli.main(["author", "screen gamers", "--out", str(tmp_path), "--simulate", "50"])
    assert rc == 0
    assert "coverage gate passed" in capsys.readouterr().out


# ----------------------------------------------------------------- CLI: reviewable output
def _author_to(tmp_path, monkeypatch):
    """Stub-author a coverage-passing survey into tmp_path and return its .survey.json path."""
    monkeypatch.setenv("SURVEY_LLM", "stub")
    cli.main(["author", "screen gamers", "--out", str(tmp_path), "--simulate", "0"])
    return next((tmp_path).glob("*.survey.json"))


def test_cli_simulate_out_writes_coverage_json(tmp_path, monkeypatch, capsys):
    survey = _author_to(tmp_path, monkeypatch)
    out = tmp_path / "cov.json"
    rc = cli.main(["simulate", str(survey), "-n", "40", "--out", str(out)])
    assert rc == 0 and out.exists()
    cov = json.loads(out.read_text())
    assert cov["n"] == 40 and "problems" in cov          # the structured coverage result, for review
    assert "report" in capsys.readouterr().out           # prints a "review the run" hint


def test_cli_pretest_out_and_report(tmp_path, monkeypatch):
    survey = _author_to(tmp_path, monkeypatch)
    monkeypatch.setenv("SURVEY_PERSONAS", "synthetic")
    run, report = tmp_path / "run.json", tmp_path / "rep.html"
    rc = cli.main(["pretest", str(survey), "--n", "3", "--out", str(run), "--report", str(report)])
    assert rc == 0
    res = json.loads(run.read_text())
    assert res["n"] == 3 and len(res["results"]) == 3    # the full persona run, for review
    assert "<html" in report.read_text().lower()         # an HTML report OF that run


def test_cli_pretest_honors_audience_n_when_no_flag(tmp_path, monkeypatch, capsys):
    # regression: --n defaults to None, so an audience file's `n` is honored unless --n overrides it
    survey = _author_to(tmp_path, monkeypatch)
    monkeypatch.setenv("SURVEY_PERSONAS", "synthetic")
    aud = tmp_path / "aud.json"
    aud.write_text(json.dumps({"n": 9, "segments": [{"weight": 1, "traits": {}}]}))
    cli.main(["pretest", str(survey), "--audience", str(aud)])           # no --n
    assert "— 9 synthetic respondents" in capsys.readouterr().out
    cli.main(["pretest", str(survey), "--audience", str(aud), "--n", "3"])  # explicit --n wins
    assert "— 3 synthetic respondents" in capsys.readouterr().out


def test_cli_schema_prints_schema(capsys):
    rc = cli.main(["schema"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"$schema"' in out and "survey" in out


# ----------------------------------------------------------------- bring-your-own-store seam
def test_store_builtins():
    assert type(store_pkg.get_store("memory")).__name__ == "MemoryStore"
    assert type(store_pkg.get_store("file", data_dir="/tmp/_sd_test_store")).__name__ == "FileStore"


def test_store_custom_import_path():
    # colon and dotted forms both resolve a deployer-supplied class
    assert type(store_pkg.get_store("survey_agent.store.memory:MemoryStore")).__name__ == "MemoryStore"
    assert type(store_pkg.get_store("survey_agent.store.memory.MemoryStore")).__name__ == "MemoryStore"


def test_store_unknown_name_raises():
    with pytest.raises(ValueError):
        store_pkg.get_store("nope")
