"""Survey-management layer: the registry sidecar, `survey-agent list`/`status`, the in-process
inline coverage gate, and the MCP save/list tools that close the loop on file-access surfaces."""
import json
import os

import pytest

from survey_agent import cli, registry


def _spec(sid="mgmt-test", **extra):
    s = {
        "id": sid, "title": "Mgmt test", "description": "A test screener",
        "settings": {"url_variables": ["uid", "rid"]},
        "pages": [{"id": "p1", "questions": [
            {"id": "q1", "type": "radio", "required": True, "title": "Pick one",
             "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]}]}],
        "actions": [{"type": "complete", "redirect": {"kind": "default"}}],
    }
    s.update(extra)
    return {"survey": s}


def _write(tmp_path, spec):
    p = tmp_path / f"{spec['survey']['id']}.survey.json"
    p.write_text(json.dumps(spec))
    return p


# ----------------------------------------------------------------- registry module
def test_record_test_and_derived_state(tmp_path):
    cov = {"n": 50, "problems": [], "loi_seconds": 120}
    registry.record_test("s1", cov, incidence=0.47, sdir=str(tmp_path))
    entry = registry.load(str(tmp_path))["surveys"]["s1"]
    assert entry["tested"]["gate"] == "passed"
    assert entry["tested"]["incidence"] == 0.47 and entry["tested"]["loi_min"] == 2.0
    assert registry.derived_state(entry) == "tested"


def test_failed_gate_and_explicit_status_override(tmp_path):
    registry.record_test("s2", {"n": 10, "problems": ["dead branch"]}, sdir=str(tmp_path))
    e = registry.load(str(tmp_path))["surveys"]["s2"]
    assert registry.derived_state(e) == "tested(failing)"
    registry.set_status("s2", status="live", url="https://x/s2", sdir=str(tmp_path))
    e = registry.load(str(tmp_path))["surveys"]["s2"]
    assert registry.derived_state(e) == "live" and e["live"]["url"] == "https://x/s2"


def test_catalog_merges_file_metadata_and_state(tmp_path):
    _write(tmp_path, _spec("cat1", tags=["screener", "q3"], owner="alex"))
    registry.record_validation("cat1", ok=True, sdir=str(tmp_path))
    cat = registry.catalog(str(tmp_path))
    e = next(x for x in cat if x["id"] == "cat1")
    assert e["description"] == "A test screener" and e["tags"] == ["screener", "q3"]
    assert e["owner"] == "alex" and e["state"] == "validated" and e["file"] == "cat1.survey.json"


def test_catalog_surfaces_registry_entry_without_a_file(tmp_path):
    registry.set_status("ghost", status="closed", sdir=str(tmp_path))
    e = next(x for x in registry.catalog(str(tmp_path)) if x["id"] == "ghost")
    assert e["missing_file"] is True and e["state"] == "closed"


# ----------------------------------------------------------------- schema: tags/owner accepted
def test_schema_accepts_tags_and_owner():
    from survey_agent.engine import validate as v
    errs = [e for e in v.validate(_spec(tags=["a", "b"], owner="alex")) if not e.startswith("[warn]")]
    assert errs == []


# ----------------------------------------------------------------- CLI: list / status
def test_cli_list_json_and_status_roundtrip(tmp_path, capsys):
    _write(tmp_path, _spec("clitest"))
    assert cli.main(["list", "--dir", str(tmp_path), "--json"]) == 0
    cat = json.loads(capsys.readouterr().out)
    assert any(e["id"] == "clitest" for e in cat)

    assert cli.main(["status", "clitest", "--dir", str(tmp_path),
                     "--set", "approved", "--note", "ready to field"]) == 0
    capsys.readouterr()
    assert cli.main(["status", "clitest", "--dir", str(tmp_path)]) == 0
    entry = json.loads(capsys.readouterr().out)
    assert entry["state"] == "approved" and entry["note"] == "ready to field"


def test_cli_launch_emits_url_and_marks_live(tmp_path, capsys):
    survey = _write(tmp_path, _spec("launchtest"))
    assert cli.main(["launch", str(survey), "--base", "https://my.example.com"]) == 0
    out = capsys.readouterr().out
    assert "https://my.example.com/s/launchtest?uid={uid}&rid={rid}" in out
    e = next(x for x in registry.catalog(str(tmp_path)) if x["id"] == "launchtest")
    assert e["state"] == "live" and e["live"]["url"].startswith("https://my.example.com/s/launchtest")


def test_cli_launch_no_live_just_prints(tmp_path, capsys):
    survey = _write(tmp_path, _spec("launchpreview"))
    assert cli.main(["launch", str(survey), "--no-live"]) == 0
    assert "/s/launchpreview?" in capsys.readouterr().out
    e = next(x for x in registry.catalog(str(tmp_path)) if x["id"] == "launchpreview")
    assert e["state"] == "draft"          # --no-live didn't flip it


def test_cli_launch_prints_operator_responsibilities(tmp_path, capsys):
    survey = _write(tmp_path, _spec("launchresp"))
    assert cli.main(["launch", str(survey), "--no-live"]) == 0
    out = capsys.readouterr().out
    assert "Before you field to real people" in out
    assert "Erasure / DSAR" in out


def test_launch_warns_that_marking_live_does_not_publish(tmp_path, capsys, monkeypatch):
    # Plain launch marks live LOCALLY but must make the publish gap impossible to miss.
    monkeypatch.delenv("SURVEY_DEPLOY_COMMAND", raising=False)
    survey = _write(tmp_path, _spec("gaptest"))
    assert cli.main(["launch", str(survey), "--base", "https://x.example"]) == 0
    out = capsys.readouterr().out
    assert "did NOT publish" in out
    assert "deploy" in out.lower()                     # points the operator at the deploy step


def test_launch_deploy_without_a_command_errors_helpfully(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SURVEY_DEPLOY_COMMAND", raising=False)
    monkeypatch.chdir(tmp_path)                         # no survey-agent.config.yaml here
    survey = _write(tmp_path, _spec("nohook"))
    assert cli.main(["launch", str(survey), "--base", "https://x.example", "--deploy"]) == 1
    out = capsys.readouterr().out
    assert "needs a deploy command" in out
    # still marked live — only publishing was skipped
    assert next(x for x in registry.catalog(str(tmp_path)) if x["id"] == "nohook")["state"] == "live"


def test_launch_deploy_runs_the_configured_command(tmp_path, capsys, monkeypatch):
    sentinel = tmp_path / "DEPLOYED"
    monkeypatch.setenv("SURVEY_DEPLOY_COMMAND", f"touch {sentinel}")
    survey = _write(tmp_path, _spec("withhook"))
    assert cli.main(["launch", str(survey), "--base", "https://x.example", "--deploy"]) == 0
    assert sentinel.exists()                           # the hook actually ran
    assert "Published" in capsys.readouterr().out
    assert next(x for x in registry.catalog(str(tmp_path)) if x["id"] == "withhook")["state"] == "live"


def test_launch_deploy_conflicts_with_no_live(tmp_path, capsys):
    survey = _write(tmp_path, _spec("conflict"))
    assert cli.main(["launch", str(survey), "--deploy", "--no-live"]) == 1
    assert "can't be combined" in capsys.readouterr().out


def test_launch_deploy_reads_command_from_config_yaml(tmp_path, capsys, monkeypatch):
    sentinel = tmp_path / "VIA_CONFIG"
    (tmp_path / "survey-agent.config.yaml").write_text(f'deploy: {{ command: "touch {sentinel}" }}\n')
    monkeypatch.delenv("SURVEY_DEPLOY_COMMAND", raising=False)
    monkeypatch.chdir(tmp_path)                          # config is resolved from the cwd
    survey = _write(tmp_path, _spec("cfghook"))
    assert cli.main(["launch", str(survey), "--base", "https://x.example", "--deploy"]) == 0
    assert sentinel.exists()


def test_help_command_prints_the_flow(capsys):
    assert cli.main(["help"]) == 0
    out = capsys.readouterr().out
    assert "THE FLOW" in out and "survey-agent author" in out


def test_bare_invocation_prints_guided_help(capsys):
    assert cli.main([]) == 0                            # no subcommand → guided help, not an argparse error
    assert "THE FLOW" in capsys.readouterr().out


def _fake_checkout(tmp_path):
    """Make tmp_path look like a *second* survey_agent source checkout (src/survey_agent/__init__.py)."""
    pkg = tmp_path / "src" / "survey_agent"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")


def test_doctor_clean_when_not_a_second_checkout(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)                         # not a survey_agent source tree
    assert cli.main(["doctor"]) == 0
    assert "survey-agent doctor" in capsys.readouterr().out


def test_doctor_flags_multi_clone_mismatch(tmp_path, capsys, monkeypatch):
    _fake_checkout(tmp_path)                            # a DIFFERENT checkout than the imported one
    monkeypatch.chdir(tmp_path)
    assert cli.main(["doctor"]) == 1                   # non-zero so scripts/CI can catch it
    assert "MISMATCH" in capsys.readouterr().out


def test_startup_guard_warns_on_mismatch(tmp_path, capsys, monkeypatch):
    _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SURVEY_AGENT_NO_DOCTOR", raising=False)
    cli.main(["list", "--dir", str(tmp_path)])
    assert "running code from" in capsys.readouterr().err   # warning goes to stderr, stdout stays clean


def test_startup_guard_can_be_silenced(tmp_path, capsys, monkeypatch):
    _fake_checkout(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SURVEY_AGENT_NO_DOCTOR", "1")
    cli.main(["list", "--dir", str(tmp_path)])
    assert "running code from" not in capsys.readouterr().err


def test_gated_launch_needs_responsibility_ack_to_go_live(tmp_path, capsys, monkeypatch):
    # With the launch token gate on, the right token is necessary but NOT sufficient — the operator
    # must also acknowledge the pre-fielding checklist before a survey flips live.
    monkeypatch.setenv("SURVEY_LAUNCH_APPROVAL_TOKEN", "sekret")
    monkeypatch.delenv("SURVEY_OPERATOR_ACK", raising=False)
    survey = _write(tmp_path, _spec("gated"))

    assert cli.main(["launch", str(survey), "--base", "https://x.example", "--approve", "sekret"]) == 0
    assert "not marked live" in capsys.readouterr().out.lower()
    assert next(x for x in registry.catalog(str(tmp_path)) if x["id"] == "gated")["state"] == "draft"

    assert cli.main(["launch", str(survey), "--base", "https://x.example",
                     "--approve", "sekret", "--ack-responsibilities"]) == 0
    assert next(x for x in registry.catalog(str(tmp_path)) if x["id"] == "gated")["state"] == "live"


def test_cli_status_rejects_unknown_state(tmp_path, capsys):
    _write(tmp_path, _spec("clitest2"))
    assert cli.main(["status", "clitest2", "--dir", str(tmp_path), "--set", "bogus"]) == 1
    assert "unknown state" in capsys.readouterr().out


def test_cli_simulate_records_tested_state(tmp_path, capsys):
    survey = _write(tmp_path, _spec("simrec"))
    assert cli.main(["simulate", str(survey), "-n", "20"]) == 0
    assert "coverage gate passed" in capsys.readouterr().out
    e = next(x for x in registry.catalog(str(tmp_path)) if x["id"] == "simrec")
    assert e["state"] == "tested" and e["tested"]["gate"] == "passed"


# ----------------------------------------------------------------- MCP: save_survey / inline simulate / list
def test_mcp_save_validate_simulate_list(tmp_path, monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")  # the real SDK, not the local survey_agent.mcp shadow
    monkeypatch.setenv("SURVEY_LLM", "stub")
    import survey_agent.mcp.server as mcp
    monkeypatch.setattr(mcp, "SURVEYS_DIR", str(tmp_path))

    # invalid spec is refused (no file written)
    bad = mcp.save_survey({"survey": {"id": "bad"}})        # missing required title/pages/actions
    assert bad["ok"] is False and bad["saved"] is False
    assert not (tmp_path / "bad.survey.json").exists()

    # valid spec is persisted + validated-recorded
    res = mcp.save_survey(_spec("mcpsave"))
    assert res["ok"] and res["saved"] and (tmp_path / "mcpsave.survey.json").exists()

    # inline coverage gate works with NO saved file (the A+B fix)
    out = mcp.simulate_survey(survey_json=_spec("inline"))
    assert "coverage gate passed" in out
    assert not (tmp_path / "inline.survey.json").exists()

    # simulate a saved survey by id → records tested
    mcp.simulate_survey(survey_path="mcpsave", n=20)

    cat = mcp.list_surveys()
    e = next(x for x in cat if x["id"] == "mcpsave")
    assert e["state"] == "tested"


def test_mcp_begin_survey_accepts_inline_spec(tmp_path, monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")  # the real SDK, not the local survey_agent.mcp shadow
    monkeypatch.setenv("SURVEY_LLM", "stub")
    import survey_agent.mcp.server as mcp
    monkeypatch.setattr(mcp, "SURVEYS_DIR", str(tmp_path))
    out = mcp.begin_survey(survey_json=_spec("inlinebegin"))
    assert out["session_token"].startswith("sv_") and out["message"]


# ----------------------------------------------------------------- dashboard (the 3 tiers)
def test_dashboard_render_html():
    from survey_agent import dashboard
    cat = [{"id": "alpha", "title": "Alpha", "description": "first", "tags": ["x"], "version": 1,
            "state": "tested", "tested": {"incidence": 0.4, "loi_min": 2.0, "at": "2026-06-09T00:00:00Z"}},
           {"id": "beta", "state": "live", "live": {"url": "https://x/beta"}}]
    html = dashboard.render_html(cat, report_links={"alpha": "alpha-report.html"})
    assert "<html" in html and "alpha" in html and "beta" in html
    assert "tested" in html and "live" in html
    assert 'href="alpha-report.html"' in html          # deep-link to the report
    assert dashboard.render_html([]).count("No surveys") == 1   # empty state


def test_cli_dashboard_writes_html_with_reports(tmp_path):
    _write(tmp_path, _spec("dashtest"))
    out = tmp_path / "dash.html"
    assert cli.main(["dashboard", "--dir", str(tmp_path), "--out", str(out), "--simulate", "20"]) == 0
    html = out.read_text()
    assert "dashtest" in html and "dashtest-report.html" in html
    assert (tmp_path / "dashtest-report.html").exists()     # the linked report was generated


def test_mcp_build_dashboard(tmp_path, monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")  # the real SDK, not the local survey_agent.mcp shadow
    monkeypatch.setenv("SURVEY_LLM", "stub")
    import survey_agent.mcp.server as mcp
    monkeypatch.setattr(mcp, "SURVEYS_DIR", str(tmp_path))
    _write(tmp_path, _spec("mcpdash"))
    res = mcp.build_dashboard(out=str(tmp_path / "d.html"))
    assert res["surveys"] >= 1 and os.path.exists(res["path"])
    assert "mcpdash" in open(res["path"]).read()
