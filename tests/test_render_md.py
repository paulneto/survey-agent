"""JSON → readable Markdown (the researcher-reviewable version) — round-trips via compile, carries
logic notes, and is emitted by every creation path (`author`, the `render` command)."""
import json

from survey_agent import cli
from survey_agent.engine.compile_md import compile_markdown, render_markdown


def _spec():
    return {"survey": {
        "id": "rt", "title": "Round-trip", "description": "a screener", "tags": ["screener"],
        "settings": {"url_variables": ["uid", "rid"]},
        "pages": [
            {"id": "p1", "questions": [
                {"id": "q_use", "type": "radio", "required": True, "title": "Do you use it?",
                 "options": [{"value": "current", "label": "Currently"}, {"value": "never", "label": "Never"}]}]},
            {"id": "p2", "display_logic": {"ref": {"kind": "question", "id": "q_use"}, "operator": "equals", "value": "current"},
             "questions": [{"id": "q_why", "type": "textarea", "title": "Why {{q:q_use.answer}}?"}]}],
        "quotas": [{"id": "cap", "scope": "answer", "ref": {"kind": "question", "id": "q_use"},
                    "match": ["current"], "limit": 300}],
        "actions": [
            {"type": "disqualify", "reason_code": "non_user",
             "when": {"ref": {"kind": "question", "id": "q_use"}, "operator": "equals", "value": "never"}},
            {"type": "complete", "redirect": {"kind": "default"}}],
    }}


def test_render_markdown_roundtrips():
    spec = _spec()
    recompiled = compile_markdown(render_markdown(spec))
    assert recompiled == spec                      # frontmatter is canonical → exact round-trip


def test_render_markdown_has_logic_notes():
    md = render_markdown(_spec())
    assert "# Round-trip" in md and "a screener" in md
    assert "Screen out" in md and "non_user" in md              # disqualify annotated
    assert "shown only when" in md.lower() and "q_use" in md    # display logic annotated
    assert "cap" in md and "300" in md                         # quota annotated
    assert "Piped" in md                                        # {{q:...}} surfaced
    assert "Complete" in md                                     # terminal annotated


def test_author_emits_readable_md(tmp_path, monkeypatch):
    monkeypatch.setenv("SURVEY_LLM", "stub")
    cli.main(["author", "screen gamers", "--out", str(tmp_path), "--simulate", "0"])
    js = next(tmp_path.glob("*.survey.json"))
    md = js.with_name(js.name.replace(".survey.json", ".md"))
    assert md.exists(), "author should also write the readable .md"
    # the emitted .md compiles back to the same survey JSON
    assert compile_markdown(md.read_text()) == json.loads(js.read_text())


def test_md_to_html_wrapped_list_items_stay_one_list():
    """A numbered/bulleted item that wraps onto continuation lines must NOT split the list
    (which restarted <ol> numbering → every item rendered "1.")."""
    from survey_agent.render import md_to_html
    ol = md_to_html("1. First item that wraps\n   onto a second line.\n2. Second.\n3. Third.")
    assert ol.count("<ol>") == 1 and ol.count("<li>") == 3
    assert "<p>" not in ol                                  # continuation folded in, not a stray paragraph
    ul = md_to_html("- one that wraps\n  here\n- two")
    assert ul.count("<ul>") == 1 and ul.count("<li>") == 2


def test_render_command_writes_md(tmp_path):
    js = tmp_path / "rt.survey.json"
    js.write_text(json.dumps(_spec()))
    assert cli.main(["render", str(js), "--write"]) == 0
    assert (tmp_path / "rt.md").exists()
