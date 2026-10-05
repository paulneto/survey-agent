"""Generated reports/exports go to a configurable output dir, not the cwd."""
import os


def test_default_is_reports(monkeypatch):
    monkeypatch.delenv("SURVEY_OUTPUT_DIR", raising=False)
    monkeypatch.delenv("SURVEY_CONFIG", raising=False)
    from survey_agent import cli
    monkeypatch.setattr(cli, "_load_operator_config", lambda: {})
    assert cli._output_dir() == "reports"


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("SURVEY_OUTPUT_DIR", "/tmp/sa-out")
    from survey_agent import cli
    assert cli._output_dir() == "/tmp/sa-out"


def test_config_used_when_no_env(monkeypatch):
    monkeypatch.delenv("SURVEY_OUTPUT_DIR", raising=False)
    from survey_agent import cli
    monkeypatch.setattr(cli, "_load_operator_config", lambda: {"output": {"dir": "build/out"}})
    assert cli._output_dir() == "build/out"


def test_default_out_creates_dir_and_joins(tmp_path, monkeypatch):
    monkeypatch.setenv("SURVEY_OUTPUT_DIR", str(tmp_path / "rpts"))
    from survey_agent import cli
    p = cli._default_out("x-report.html")
    assert p == os.path.join(str(tmp_path / "rpts"), "x-report.html")
    assert os.path.isdir(str(tmp_path / "rpts"))           # created on demand
