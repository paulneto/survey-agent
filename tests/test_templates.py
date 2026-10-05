import json

import pytest

from survey_agent import templates as T
from survey_agent.engine import validate as v

CORE = ["blank", "screener", "explainer", "feedback-nps", "data-download"]

_MANIFEST = ("title: T\ntype: custom\n"
             "params:\n  - { name: slug, required: true }\n  - { name: title, required: true }\n")


def _write_template(root, tid, survey, manifest=_MANIFEST):
    d = root / tid
    d.mkdir(parents=True)
    (d / "template.yaml").write_text(manifest)
    (d / "survey.json").write_text(json.dumps(survey))
    return d


@pytest.mark.parametrize("tid", CORE)
def test_core_template_scaffolds_to_a_valid_survey(tid):
    t = T.get(tid)
    assert t and t["source"] == "core"
    spec = T.render(t, {"slug": "smoke", "title": "Smoke"})
    assert spec["survey"]["id"] == "smoke" and spec["survey"]["title"] == "Smoke"
    real = [e for e in v.validate(spec) if not e.startswith("[warn]")]
    assert real == [], real


def test_missing_required_param_is_reported():
    assert "slug" in T.missing_params(T.get("explainer"), {"title": "x"})


def test_overlay_template_dir_is_discovered_and_wins(tmp_path, monkeypatch):
    survey = {"survey": {"id": "${slug}", "title": "${title}",
                         "pages": [{"id": "p", "questions": [
                             {"id": "q", "type": "radio", "options": [{"value": "a"}]}]}],
                         "actions": [{"type": "complete"}]}}
    _write_template(tmp_path, "explainer", survey)            # shadows a core template by id
    monkeypatch.setenv("SURVEY_TEMPLATES_DIR", str(tmp_path))
    t = T.get("explainer")
    assert t["source"] == "overlay"                           # overlay wins over core
    assert v.semantic_errors(T.render(t, {"slug": "s", "title": "Tt"})) == []


def test_param_values_are_json_escaped(tmp_path, monkeypatch):
    # a value with a quote/backslash/newline would corrupt naive text substitution into JSON.
    survey = {"survey": {"id": "${slug}", "title": "${title}",
                         "pages": [{"id": "p", "questions": [
                             {"id": "q", "type": "instructions", "body": "${msg}"}]}],
                         "actions": [{"type": "complete"}]}}
    man = (_MANIFEST + "  - { name: msg, default: \"\" }\n")
    _write_template(tmp_path, "esc", survey, manifest=man)
    monkeypatch.setenv("SURVEY_TEMPLATES_DIR", str(tmp_path))
    tricky = 'He said "hi"\\done\nnext'
    spec = T.render(T.get("esc"), {"slug": "s", "title": "Tt", "msg": tricky})
    assert spec["survey"]["pages"][0]["questions"][0]["body"] == tricky   # round-trips intact


def test_unresolved_token_raises(tmp_path, monkeypatch):
    survey = {"survey": {"id": "${slug}", "title": "${title}", "description": "${undeclared}",
                         "pages": [], "actions": [{"type": "complete"}]}}
    _write_template(tmp_path, "bad", survey)
    monkeypatch.setenv("SURVEY_TEMPLATES_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="undeclared"):
        T.render(T.get("bad"), {"slug": "s", "title": "t"})


def test_malformed_template_is_skipped_not_fatal(tmp_path, monkeypatch):
    good = {"survey": {"id": "${slug}", "title": "${title}", "pages": [],
                       "actions": [{"type": "complete"}]}}
    _write_template(tmp_path, "good", good)
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "template.yaml").write_text("foo: [unclosed\n")   # invalid YAML
    (broken / "survey.json").write_text("{not json")
    monkeypatch.setenv("SURVEY_TEMPLATES_DIR", str(tmp_path))
    ids = {t["id"] for t in T.list_templates()}
    assert "good" in ids and "broken" not in ids               # one bad template doesn't break discovery


def test_mcp_template_tools(tmp_path, monkeypatch):
    """The chat/cowork surface: list_templates + new_survey mirror `survey-agent new`."""
    pytest.importorskip("mcp.server.fastmcp")                   # skip if the [mcp] extra isn't installed (or shadowed)
    import survey_agent.mcp.server as mcp
    monkeypatch.setattr(mcp, "SURVEYS_DIR", str(tmp_path))      # SURVEYS_DIR is a module global, captured at import
    list_templates = getattr(mcp.list_templates, "fn", mcp.list_templates)
    new_survey = getattr(mcp.new_survey, "fn", mcp.new_survey)

    assert "screener" in [t["id"] for t in list_templates()]
    r = new_survey(template_id="screener", slug="mcp_scr", title="X")
    assert r["ok"] and r["saved"] and (tmp_path / "mcp_scr.survey.json").exists()
    assert not new_survey(template_id="screener", slug="bad slug")["ok"]   # invalid slug rejected
    assert not new_survey(template_id="does-not-exist", slug="x")["ok"]    # unknown template rejected
