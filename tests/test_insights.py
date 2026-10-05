"""Auto-insight — the LLM analyst read-out built on the report's aggregated stats. Offline via the
deterministic stub; structure + embedding verified without a key."""
import json
import os

import survey_agent.ai as ai
from survey_agent import cli, report as report_mod, insights as insights_mod
from survey_agent.engine.runtime import SurveyRuntime

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "src", "survey_agent", "examples", "example.survey.json")


def _setup(n=120):
    rt = SurveyRuntime(json.load(open(EXAMPLE)))
    rows, quotas = report_mod.collect_via_simulation(rt, n)
    return rt, rows, report_mod.build_report(rt, rows, quotas)


def test_stub_insights_has_the_full_shape():
    rt, rows, rep = _setup()
    ins = ai.StubLLM().insights(rep, insights_mod.open_ends(rt, rows))
    for k in ("headline", "findings", "themes", "watchouts", "next_steps", "engine"):
        assert k in ins
    assert ins["engine"] == "stub"
    assert ins["headline"] and ins["findings"]                 # mechanical but non-empty
    assert any("incidence" in f for f in ins["findings"])


def test_generate_insights_offline(monkeypatch):
    monkeypatch.setenv("SURVEY_LLM", "stub")
    rt, rows, rep = _setup()
    ins = insights_mod.generate_insights(rt, rows, rep)
    assert ins["headline"] and isinstance(ins["findings"], list)


def test_render_html_and_text():
    rt, rows, rep = _setup()
    ins = ai.StubLLM().insights(rep, None)
    html = insights_mod.render_insights_html(ins)
    assert "AI insights" in html and "Key findings" in html and ins["headline"][:10] in html
    txt = insights_mod.to_text(ins)
    assert "AI insights" in txt and "Key findings" in txt


def test_report_embeds_insights_only_when_requested(monkeypatch):
    monkeypatch.setenv("SURVEY_LLM", "stub")
    rt = SurveyRuntime(json.load(open(EXAMPLE)))
    with_ins = report_mod.generate(rt, simulate_n=80, with_insights=True)
    assert "🤖 AI insights" in with_ins and "incidence" in with_ins
    assert "🤖 AI insights" not in report_mod.generate(rt, simulate_n=80, with_insights=False)


def test_cli_insights_prints_readout(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SURVEY_LLM", "stub")
    rc = cli.main(["insights", EXAMPLE, "--simulate", "80"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "AI insights" in out and "Key findings" in out
