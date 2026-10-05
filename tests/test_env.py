"""Robustness: a `.env` copied from `.env.example` (or written with inline `# comments`) must not
pollute selector values. Found during the new-user shakedown — VSCode injects `.env` verbatim, so a
trailing comment became part of the value and crashed the seam factories."""
import os

import survey_agent
from survey_agent import integrations
from survey_agent.personas import SyntheticPersonaSource, get_persona_source
from survey_agent.store import get_store


def test_env_select_strips_inline_comment_and_whitespace(monkeypatch):
    monkeypatch.setenv("SURVEY_PERSONAS", "synthetic                 # synthetic (LLM/stub) | your_pkg")
    assert survey_agent.env_select("SURVEY_PERSONAS", "x") == "synthetic"
    monkeypatch.delenv("SURVEY_PERSONAS", raising=False)
    assert survey_agent.env_select("SURVEY_PERSONAS", "fallback") == "fallback"


def test_seams_tolerate_a_polluted_env_value(monkeypatch):
    monkeypatch.setenv("SURVEY_PERSONAS", "synthetic   # comment")
    assert isinstance(get_persona_source(), SyntheticPersonaSource)
    monkeypatch.setenv("SURVEY_STORE", "memory   # memory | file | mongo")
    assert type(get_store()).__name__ == "MemoryStore"
    monkeypatch.setenv("SURVEY_INTEGRATION", "none   # none | redirect | webhook")
    assert type(integrations.get_integration()).__name__ == "Integration"


def test_load_dotenv_strips_inline_comments(tmp_path, monkeypatch):
    from survey_agent import cli
    env = tmp_path / ".env"
    env.write_text('SURVEY_PERSONAS=synthetic   # comment\nQUOTED_KEY="sk # not a comment"\n')
    monkeypatch.delenv("SURVEY_PERSONAS", raising=False)
    monkeypatch.delenv("QUOTED_KEY", raising=False)
    try:
        cli._load_dotenv(str(env))
        assert os.environ["SURVEY_PERSONAS"] == "synthetic"          # inline comment stripped
        assert os.environ["QUOTED_KEY"] == "sk # not a comment"      # # inside quotes is kept
    finally:
        os.environ.pop("SURVEY_PERSONAS", None)
        os.environ.pop("QUOTED_KEY", None)


def test_load_env_files_finds_dotenv_beside_survey_dir(tmp_path, monkeypatch):
    # the MCP server is spawned from an arbitrary cwd; it must still find the project's .env via the
    # parent of SURVEY_DIR (so AI-backed tools get the key without a manual `export`).
    proj = tmp_path / "proj"
    (proj / "surveys").mkdir(parents=True)
    (proj / ".env").write_text("ANTHROPIC_API_KEY=sk-beside-surveydir\n")
    monkeypatch.chdir(tmp_path)                       # NOT the project dir — like Claude Code
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("SURVEY_DIR", str(proj / "surveys"))
    try:
        loaded = survey_agent.load_env_files()
        assert any(str(proj / ".env") == p for p in loaded)
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-beside-surveydir"
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def test_load_env_files_does_not_override_exported(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    (proj / "surveys").mkdir(parents=True)
    (proj / ".env").write_text("ANTHROPIC_API_KEY=sk-from-file\n")
    monkeypatch.setenv("SURVEY_DIR", str(proj / "surveys"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-exported")
    survey_agent.load_env_files()
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-exported"   # a real env var wins over .env


def test_dotenv_fills_unexpanded_placeholder_and_empty(tmp_path, monkeypatch):
    # an MCP client may pass `"KEY": "${KEY}"` literally (or expand it to "") when the shell var is
    # unset — that junk value must NOT block .env from supplying the real key.
    env = tmp_path / ".env"
    env.write_text("ANTHROPIC_API_KEY=sk-real\nSURVEY_STORE=file\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "${ANTHROPIC_API_KEY}")   # literal, unexpanded
    monkeypatch.setenv("SURVEY_STORE", "")                            # empty
    survey_agent.load_dotenv(str(env))
    assert os.environ["ANTHROPIC_API_KEY"] == "sk-real"
    assert os.environ["SURVEY_STORE"] == "file"


def test_mcp_json_example_is_valid_and_safe():
    import json
    path = os.path.join(os.path.dirname(__file__), "..", ".mcp.json.example")
    cfg = json.load(open(path))
    server = cfg["mcpServers"]["survey-agent"]
    assert server["command"].endswith("survey-agent-mcp")
    # the template must never ship a literal key — only the ${...} shell-expansion form
    assert server["env"]["ANTHROPIC_API_KEY"].startswith("${")


def test_env_example_has_no_inline_comments_on_value_lines():
    # guard: `cp .env.example .env` must yield clean values — comments live on their own lines only
    path = os.path.join(os.path.dirname(__file__), "..", ".env.example")
    for ln in open(path):
        s = ln.rstrip("\n")
        if "=" in s and not s.lstrip().startswith("#"):
            value = s.split("=", 1)[1]
            assert "#" not in value, f"inline comment on a value line: {s!r}"
