"""Tests for `survey-agent copy` (duplicate a survey) and `survey-agent templatize` (promote a survey
into a reusable template). Both are invoked through `cli.main([...])` like the other CLI tests."""
import json

from survey_agent import cli, registry, templates as T
from survey_agent.cli import EXAMPLE
from survey_agent.engine import validate as v


def _seed_source(sdir, slug="welcome", version=3, title="Welcome", live=True):
    """Write a known-good survey (the packaged example) into `sdir` as `<slug>.survey.json`, optionally
    with a 'live' lifecycle entry in the sidecar — to prove a copy starts a fresh draft regardless."""
    spec = json.load(open(EXAMPLE))
    s = spec["survey"]
    s["id"], s["version"], s["title"] = slug, version, title
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / f"{slug}.survey.json").write_text(json.dumps(spec, indent=2))
    if live:
        registry.set_status(slug, status="live", sdir=str(sdir))
    return spec


# ----------------------------------------------------------------- copy
def test_copy_by_slug_creates_fresh_draft(tmp_path, monkeypatch):
    sdir = tmp_path / "surveys"
    src = _seed_source(sdir, slug="welcome", version=3)
    monkeypatch.setenv("SURVEY_DIR", str(sdir))

    rc = cli.main(["copy", "welcome", "--slug", "welcome-feb", "--title", "Welcome (Feb)"])
    assert rc == 0
    out = sdir / "welcome-feb.survey.json"
    assert out.exists()
    new = json.load(open(out))["survey"]

    assert new["id"] == "welcome-feb"
    assert new["title"] == "Welcome (Feb)"
    assert new["version"] == 1                                   # fresh lineage, not the donor's v3
    assert new["pages"] == src["survey"]["pages"]                # content carried verbatim
    assert (sdir / "welcome-feb.md").exists()                   # readable companion written


def test_copy_does_not_inherit_live_state(tmp_path, monkeypatch):
    sdir = tmp_path / "surveys"
    _seed_source(sdir, slug="welcome", live=True)
    monkeypatch.setenv("SURVEY_DIR", str(sdir))

    cli.main(["copy", "welcome", "--slug", "welcome-copy"])
    reg = registry.load(str(sdir))["surveys"]
    assert registry.derived_state(reg["welcome"]) == "live"     # source untouched
    assert registry.derived_state(reg.get("welcome-copy", {})) != "live"  # copy is a clean draft


def test_copy_keeps_source_title_when_no_title_flag(tmp_path, monkeypatch):
    sdir = tmp_path / "surveys"
    _seed_source(sdir, slug="welcome", title="Original Title")
    monkeypatch.setenv("SURVEY_DIR", str(sdir))
    cli.main(["copy", "welcome", "--slug", "dup"])
    assert json.load(open(sdir / "dup.survey.json"))["survey"]["title"] == "Original Title"


def test_copy_by_path_into_explicit_out(tmp_path, monkeypatch):
    sdir = tmp_path / "surveys"
    _seed_source(sdir, slug="welcome")
    out = tmp_path / "out"
    rc = cli.main(["copy", str(sdir / "welcome.survey.json"), "--slug", "viapath", "--out", str(out)])
    assert rc == 0 and (out / "viapath.survey.json").exists()


def test_copy_missing_source_and_bad_slug_are_rejected(tmp_path, monkeypatch):
    sdir = tmp_path / "surveys"
    _seed_source(sdir, slug="welcome")
    monkeypatch.setenv("SURVEY_DIR", str(sdir))
    assert cli.main(["copy", "nope", "--slug", "x"]) == 2          # unknown source
    assert cli.main(["copy", "welcome", "--slug", "bad slug"]) == 2  # invalid slug


def test_copy_refuses_to_clobber_without_force(tmp_path, monkeypatch):
    sdir = tmp_path / "surveys"
    _seed_source(sdir, slug="welcome")
    monkeypatch.setenv("SURVEY_DIR", str(sdir))
    assert cli.main(["copy", "welcome", "--slug", "dup"]) == 0
    assert cli.main(["copy", "welcome", "--slug", "dup"]) == 2     # exists → refuse
    assert cli.main(["copy", "welcome", "--slug", "dup", "--force"]) == 0  # --force overwrites


# ----------------------------------------------------------------- templatize
def test_templatize_roundtrips_through_new(tmp_path, monkeypatch):
    sdir = tmp_path / "surveys"
    _seed_source(sdir, slug="welcome")
    monkeypatch.setenv("SURVEY_DIR", str(sdir))
    tdir = tmp_path / "templates"

    rc = cli.main(["templatize", "welcome", "--id", "welcome-tpl", "--out", str(tdir)])
    assert rc == 0
    man = (tdir / "welcome-tpl" / "template.yaml").read_text()
    spec_text = (tdir / "welcome-tpl" / "survey.json").read_text()
    assert "${slug}" in spec_text and "${title}" in spec_text     # id/title tokenized
    assert "theme: paper" in man                                  # theme lifted from settings
    assert "url_variables" in man                                 # url vars lifted into requires

    # the template is discoverable and scaffolds back to a valid survey
    monkeypatch.setenv("SURVEY_TEMPLATES_DIR", str(tdir))
    t = T.get("welcome-tpl")
    assert t and t["source"] == "overlay"
    spec = T.render(t, {"slug": "welcome2", "title": "Welcome 2"})
    assert spec["survey"]["id"] == "welcome2" and spec["survey"]["title"] == "Welcome 2"
    assert [e for e in v.validate(spec) if not e.startswith("[warn]")] == []


def test_templatize_default_id_from_filename(tmp_path, monkeypatch):
    sdir = tmp_path / "surveys"
    _seed_source(sdir, slug="welcome")
    monkeypatch.setenv("SURVEY_DIR", str(sdir))
    tdir = tmp_path / "templates"
    cli.main(["templatize", "welcome", "--out", str(tdir)])
    assert (tdir / "welcome" / "template.yaml").exists()          # id defaulted to the source slug


def test_templatize_refuses_existing_dir_without_force(tmp_path, monkeypatch):
    sdir = tmp_path / "surveys"
    _seed_source(sdir, slug="welcome")
    monkeypatch.setenv("SURVEY_DIR", str(sdir))
    tdir = tmp_path / "templates"
    assert cli.main(["templatize", "welcome", "--id", "t", "--out", str(tdir)]) == 0
    assert cli.main(["templatize", "welcome", "--id", "t", "--out", str(tdir)]) == 2
    assert cli.main(["templatize", "welcome", "--id", "t", "--out", str(tdir), "--force"]) == 0


# ----------------------------------------------------------------- shared helper + MCP surface
def test_from_survey_builds_manifest_and_tokens():
    spec = json.load(open(EXAMPLE))
    spec["survey"]["tags"] = ["screener", "extra"]
    manifest, out = T.from_survey(spec, id="tpl", title="T")
    assert manifest["id"] == "tpl" and manifest["title"] == "T"
    assert manifest["type"] == "screener"                         # type defaults to the first tag
    assert {p["name"] for p in manifest["params"]} == {"slug", "title"}
    assert manifest["theme"] == "paper"                           # lifted from settings
    assert "url_variables" in manifest["requires"]                # lifted from settings
    assert out["survey"]["id"] == "${slug}" and out["survey"]["title"] == "${title}"
    assert out["survey"]["version"] == 1


def test_mcp_copy_and_templatize_tools(tmp_path, monkeypatch):
    """The chat/cowork surface: copy_survey + templatize_survey mirror the CLI commands."""
    import pytest
    pytest.importorskip("mcp.server.fastmcp")                     # skip if the [mcp] extra isn't installed
    import survey_agent.mcp.server as mcp
    sdir = tmp_path / "surveys"
    _seed_source(sdir, slug="welcome")
    monkeypatch.setattr(mcp, "SURVEYS_DIR", str(sdir))            # SURVEYS_DIR is a module global
    copy_survey = getattr(mcp.copy_survey, "fn", mcp.copy_survey)
    templatize_survey = getattr(mcp.templatize_survey, "fn", mcp.templatize_survey)

    r = copy_survey(source="welcome", slug="welcome-mcp", title="W")
    assert r["ok"] and r["saved"] and (sdir / "welcome-mcp.survey.json").exists()
    assert json.load(open(sdir / "welcome-mcp.survey.json"))["survey"]["version"] == 1
    assert not copy_survey(source="welcome", slug="welcome-mcp")["ok"]   # refuses to clobber
    assert not copy_survey(source="nope", slug="x")["ok"]               # unknown source
    assert not copy_survey(source="welcome", slug="bad slug")["ok"]     # invalid slug

    tdir = tmp_path / "templates"
    t = templatize_survey(source="welcome", template_id="welcome-tpl", dest=str(tdir))
    assert t["ok"] and (tdir / "welcome-tpl" / "template.yaml").exists()
    assert not templatize_survey(source="welcome", template_id="welcome-tpl", dest=str(tdir))["ok"]  # exists
