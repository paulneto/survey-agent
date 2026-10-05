"""Release-blocker regressions for the CLI / runtime / console / MCP surfaces (0.1.0 QA pass)."""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
from http.server import ThreadingHTTPServer

import pytest

from survey_agent import cli, config
from test_server_http import _get, _post, _survey, running_multi, running_server

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STREAMING = os.path.join(REPO, "surveys", "streaming-usage.survey.json")


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A fresh `survey-agent init` project, cwd'd into, with config state reset."""
    monkeypatch.chdir(tmp_path)
    for k in ("SURVEY_STORE", "SURVEY_INTEGRATION", "SURVEY_THEME", "SURVEY_DIR", "SURVEY_CONFIG",
              "SURVEY_LAUNCH_APPROVAL_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SURVEY_DATA_DIR"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(config, "SOURCES", {})
    monkeypatch.setattr(config, "_APPLIED", {})
    import survey_agent
    monkeypatch.setattr(survey_agent, "DOTENV_KEYS", {})
    assert cli.main(["init"]) == 0
    return tmp_path


# ------------------------------------------------------------------ .env parsing (#3)
def test_dotenv_comment_only_value_is_empty(tmp_path, monkeypatch):
    from survey_agent import load_dotenv
    monkeypatch.delenv("ZZ_KEY", raising=False)
    monkeypatch.delenv("ZZ_OTHER", raising=False)
    monkeypatch.delenv("ZZ_Q", raising=False)
    env = tmp_path / ".env"
    env.write_text("ZZ_KEY=          # Claude  (get one at console.anthropic.com)\n"
                   "ZZ_OTHER=value   # trailing\nZZ_Q='a # b'\n")
    load_dotenv(str(env))
    assert os.environ["ZZ_KEY"] == ""
    assert os.environ["ZZ_OTHER"] == "value"
    assert os.environ["ZZ_Q"] == "a # b"


def test_init_env_template_has_no_inline_comments_and_no_key(project, capsys):
    text = (project / ".env").read_text()
    for line in text.splitlines():
        if line and not line.startswith("#"):
            assert "#" not in line, line
    capsys.readouterr()
    cli.main(["doctor", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["llm_key"] is False


# ------------------------------------------------------------------ one store default (#4/#5)
def test_unset_store_and_integration_resolve_to_file_and_none(project, capsys):
    capsys.readouterr()
    cli.main(["doctor", "--json"])
    d = json.loads(capsys.readouterr().out)["config"]
    # the scaffolded config sets store.backend=file and integration=none
    assert d["SURVEY_STORE"] == "file" and d["sources"]["SURVEY_STORE"].startswith("config")
    assert d["SURVEY_INTEGRATION"] == "none" and d["sources"]["SURVEY_INTEGRATION"].startswith("config")
    assert d["SURVEY_THEME"] == "paper"


def test_builtin_defaults_without_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SURVEY_STORE", raising=False)
    monkeypatch.delenv("SURVEY_INTEGRATION", raising=False)
    monkeypatch.delenv("SURVEY_CONFIG", raising=False)
    monkeypatch.setattr(config, "SOURCES", {})
    monkeypatch.setattr(config, "_APPLIED", {})
    config.apply_defaults()
    assert os.environ["SURVEY_STORE"] == "file" and config.SOURCES["SURVEY_STORE"][0] == "default"
    assert os.environ["SURVEY_INTEGRATION"] == "none"


def test_env_beats_config(project, monkeypatch, capsys):
    monkeypatch.setenv("SURVEY_STORE", "sqlite")
    capsys.readouterr()
    cli.main(["doctor", "--json"])
    d = json.loads(capsys.readouterr().out)["config"]
    assert d["SURVEY_STORE"] == "sqlite" and d["sources"]["SURVEY_STORE"] == "environment"


def test_report_and_dsar_see_file_store_responses(project, capsys):
    from survey_agent.store.file import FileStore
    spec = json.load(open(project / "surveys" / "example.survey.json"))
    sid = spec["survey"]["id"]
    FileStore("./survey_data").record_response(sid, {
        "id": "r1", "survey_id": sid, "status": "Complete", "url_variables": {"uid": "u-42"},
        "survey_data": {}, "date_started": "2026-01-01 00:00:00", "date_submitted": "2026-01-01 00:02:00"})
    capsys.readouterr()
    assert cli.main(["report", "surveys/example.survey.json", "--out", "r.html"]) == 0
    assert "1 responses" in capsys.readouterr().out
    cli.main(["dsar", "u-42", "--json"])
    assert json.loads(capsys.readouterr().out)["count"] == 1


# ------------------------------------------------------------------ preview / sandbox (#1)
def test_serve_preview_refused_with_public(capsys):
    assert cli.main(["serve", "--preview", "--public"]) == 2
    assert "local only" in capsys.readouterr().out


def test_draft_served_only_in_preview_mode():
    with running_multi([_survey()], state={"t_http": "draft"}) as (base, _d):
        _, html = _get(f"{base}/s/t_http?uid=1&rid=2")
        assert "Not open yet" in html
    with running_multi([_survey()], state={"t_http": "draft"}, SURVEY_SERVE_STATES="*") as (base, _d):
        _, html = _get(f"{base}/s/t_http?uid=1&rid=2")
        assert "Do you consent?" in html


# ------------------------------------------------------------------ convo (#2)
def test_convo_scripted_stops_when_script_runs_out(capsys):
    rc = cli.main(["convo", "--scripted", "yes"])
    out = capsys.readouterr().out
    assert rc == 1 and "script ended" in out


def test_convo_interactive_closed_stdin_exits_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["convo"]) == 1
    assert "input closed" in capsys.readouterr().out


# ------------------------------------------------------------------ launch gate (#6)
def test_status_set_live_is_gated(project, monkeypatch, capsys):
    monkeypatch.setenv("SURVEY_LAUNCH_APPROVAL_TOKEN", "s3cret")
    sid = json.load(open(project / "surveys" / "example.survey.json"))["survey"]["id"]
    assert cli.main(["status", sid, "--set", "live"]) == 1
    assert cli.main(["status", sid, "--set", "live", "--approve", "nope"]) == 1
    from survey_agent import registry
    assert registry.catalog()[0]["state"] != "live"
    capsys.readouterr()
    assert cli.main(["status", sid, "--set", "live", "--approve", "s3cret"]) == 0
    assert registry.catalog()[0]["state"] == "live"


def test_status_set_live_ungated_for_local_operator(project):
    sid = json.load(open(project / "surveys" / "example.survey.json"))["survey"]["id"]
    assert cli.main(["status", sid, "--set", "live"]) == 0


def test_publish_is_gated(project, monkeypatch, capsys):
    monkeypatch.setenv("SURVEY_LAUNCH_APPROVAL_TOKEN", "s3cret")
    from survey_agent import registry
    assert cli.main(["publish", "surveys/example.survey.json"]) == 1
    assert "NOT marked live" in capsys.readouterr().out
    assert registry.catalog()[0]["state"] != "live"
    assert cli.main(["publish", "surveys/example.survey.json", "--approve", "s3cret"]) == 0
    assert registry.catalog()[0]["state"] == "live"


def test_launch_is_gated(project, monkeypatch):
    monkeypatch.setenv("SURVEY_LAUNCH_APPROVAL_TOKEN", "s3cret")
    from survey_agent import registry
    cli.main(["launch", "surveys/example.survey.json", "--ack-responsibilities"])
    assert registry.catalog()[0]["state"] != "live"
    cli.main(["launch", "surveys/example.survey.json", "--approve", "s3cret", "--ack-responsibilities"])
    assert registry.catalog()[0]["state"] == "live"


def test_defs_import_is_gated(project, monkeypatch):
    monkeypatch.setenv("SURVEY_LAUNCH_APPROVAL_TOKEN", "s3cret")
    from survey_agent import registry
    cli.main(["defs-import"])
    assert registry.catalog()[0]["state"] != "live"


def test_console_mark_live_is_gated(project, monkeypatch):
    from survey_agent import console
    monkeypatch.setenv("SURVEY_LAUNCH_APPROVAL_TOKEN", "s3cret")
    sid = json.load(open(project / "surveys" / "example.survey.json"))["survey"]["id"]
    with pytest.raises(console.LaunchGated):
        console.set_status(sid, "live")
    with pytest.raises(console.LaunchGated):
        console.set_status(sid, "live", approve="wrong")
    assert console.set_status(sid, "live", approve="s3cret")["state"] == "live"
    assert console.set_status(sid, "paused")["state"] == "paused"      # other states stay free


def test_console_mark_live_ungated_without_token(project):
    from survey_agent import console
    sid = json.load(open(project / "surveys" / "example.survey.json"))["survey"]["id"]
    assert console.set_status(sid, "live")["state"] == "live"


def test_mcp_cannot_mark_live_without_token(monkeypatch):
    monkeypatch.delenv("SURVEY_LAUNCH_APPROVAL_TOKEN", raising=False)
    assert config.may_mark_live("anything", surface="mcp")[0] is False
    assert config.may_mark_live(None, surface="cli")[0] is True
    monkeypatch.setenv("SURVEY_LAUNCH_APPROVAL_TOKEN", "t")
    assert config.may_mark_live("t", surface="mcp")[0] is True
    assert config.may_mark_live(None, surface="console")[0] is False


def test_mcp_launch_survey_respects_gate(monkeypatch, tmp_path):
    pytest.importorskip("mcp")
    from survey_agent.mcp import server as mcp_server
    monkeypatch.setattr(mcp_server, "SURVEYS_DIR", str(tmp_path))
    shutil.copy(STREAMING, tmp_path / "streaming-usage.survey.json")
    monkeypatch.delenv("SURVEY_LAUNCH_APPROVAL_TOKEN", raising=False)
    r = mcp_server.launch_survey(str(tmp_path / "streaming-usage.survey.json"),
                                 approval_token="x", acknowledge_responsibilities=True)
    assert r["live"] is False


# ------------------------------------------------------------------ misc
def test_version_flag(capsys):
    import survey_agent
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert survey_agent.__version__ in capsys.readouterr().out


def test_mcp_server_info_reports_survey_agent_version():
    pytest.importorskip("mcp")
    import survey_agent
    from survey_agent.mcp import server as mcp_server
    opts = mcp_server.app._mcp_server.create_initialization_options()
    assert opts.server_version == survey_agent.__version__


def test_every_subcommand_has_help(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    import argparse
    parser_lines = [ln for ln in out.splitlines() if ln.startswith("    ") and not ln.startswith("     ")]
    bare = [ln.strip() for ln in parser_lines if len(ln.split()) == 1]
    assert not bare, f"subcommands without help: {bare}"


def test_port_in_use_is_clean_error(capsys):
    from survey_agent import server
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        with pytest.raises(server.PortInUse):
            server.bind_http(ThreadingHTTPServer, server.Handler, "127.0.0.1", port)
        assert "already in use" in capsys.readouterr().err
    finally:
        s.close()


def test_serve_port_in_use_exits_nonzero_without_traceback(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    env = {**os.environ, "SURVEY_STORE": "memory", "SURVEY_AGENT_NO_DOCTOR": "1"}
    try:
        for cmd in (["serve", "--port", str(port)], ["demo", "--no-browser", "--port", str(port),
                                                       "--port-receiver", "0"]):
            p = subprocess.run([sys.executable, "-m", "survey_agent.cli", *cmd], cwd=tmp_path, env=env,
                               capture_output=True, text=True, timeout=30)
            assert p.returncode != 0, cmd
            assert "Traceback" not in p.stderr and "already in use" in p.stderr, (cmd, p.stderr)
    finally:
        s.close()


def test_terminal_page_hides_ai_analysis():
    spec = _survey()
    spec["survey"]["settings"] = {"enrichments": [{"id": "why_sentiment", "source": "llm",
                                                    "prompt": "x", "inputs": ["q_consent"]}]}
    from survey_agent import server
    from survey_agent.engine.runtime import SurveyRuntime
    rt = SurveyRuntime(spec)
    html = server.render_terminal({"enrichments": {"why_sentiment": "positive"}},
                                  {"status": "disqualified", "message": "Thanks"}, rt)
    assert "AI analysis" not in html and "why_sentiment" not in html


def test_landing_links_use_fresh_ids():
    with running_server(_survey()) as base:
        import survey_agent.server as server
        _, html = _get(base + "/")
        assert f"rid={server._RUN_TAG}-" in html


def test_parquet_blank_optional_answers(tmp_path):
    pytest.importorskip("pyarrow")
    pytest.importorskip("pandas")
    from survey_agent import exporting
    cols = ["id", "q_age", "q_note"]
    rows = [{"id": "a", "q_age": 31, "q_note": "hi"}, {"id": "b", "q_age": "", "q_note": ""}]
    out = tmp_path / "x.parquet"
    exporting.to_parquet(cols, rows, str(out))
    import pandas as pd
    df = pd.read_parquet(out)
    assert df["q_age"].isna().tolist() == [False, True]


def test_restore_is_idempotent(project, capsys):
    from survey_agent.store.file import FileStore
    st = FileStore("./survey_data")
    st.record_response("s1", {"id": "r1", "survey_id": "s1", "status": "Complete"})
    st.record_response("s1", {"id": "r2", "survey_id": "s1", "status": "Complete"})
    assert cli.main(["backup", "--out", "b.jsonl"]) == 0
    assert cli.main(["restore", "b.jsonl"]) == 0
    assert cli.main(["restore", "b.jsonl"]) == 0
    ids = [r["id"] for r in FileStore("./survey_data").list_responses(survey_id="s1")]
    assert sorted(ids) == ["r1", "r2"]


def test_display_title_strips_merge_tokens():
    from survey_agent.report import display_title
    assert display_title("Welcome {{url:group|there}}!") == "Welcome there!"
    assert display_title("Why {{q:q_fav.answer}}?") == "Why […]?"


def test_report_titles_have_no_raw_tokens():
    from survey_agent import report
    from survey_agent.engine.runtime import SurveyRuntime
    spec = _survey()
    spec["survey"]["pages"][0]["questions"][0]["title"] = "Welcome {{url:group|there}}! Consent?"
    rt = SurveyRuntime(spec)
    rows, quotas = report.collect_via_simulation(rt, 20)
    rep = report.build_report(rt, rows, quotas)
    assert all("{{" not in q["title"] for q in rep["questions"])
    assert rep["questions"][0]["title"] == "Welcome there! Consent?"


def test_simulate_and_report_agree_on_loi():
    from survey_agent import report, simulate
    from survey_agent.engine.runtime import SurveyRuntime
    spec = json.load(open(STREAMING))
    cov = simulate.coverage(spec, n=120)
    rows, quotas = report.collect_via_simulation(SurveyRuntime(spec), 120)
    rep = report.build_report(SurveyRuntime(spec), rows, quotas)
    assert rep["loi"]["median_min"] == round(cov["loi_seconds"] / 60, 1)
    html = report.render_html(rep)
    assert "min" in html and "median LOI" in html


def test_copy_and_templatize_accept_survey_id(project, capsys):
    sid = json.load(open(project / "surveys" / "example.survey.json"))["survey"]["id"]
    assert cli.main(["copy", sid, "--slug", "dup"]) == 0
    assert (project / "surveys" / "dup.survey.json").exists()
    assert cli.main(["templatize", sid, "--out", "templates"]) == 0
    assert (project / "templates" / sid / "template.yaml").exists()


def test_init_example_has_no_hardcoded_receiver_and_neutral_redirect(project):
    text = (project / "surveys" / "example.survey.json").read_text()
    assert "127.0.0.1:9000" not in text and "substack" not in text
    assert "https://example.com/thanks" in text
    assert text == open(STREAMING).read()          # identical to the shipped streaming-usage survey


def test_preflight_checklist_mentions_builtin_erasure():
    from survey_agent import preflight
    txt = preflight.checklist_text()
    assert "no built-in erasure" not in txt and "survey-agent dsar" in txt
