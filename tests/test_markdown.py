"""Formatted information pages — `instructions` (and any question's `body`) render a safe Markdown
subset (headings, bold/italic, bullet/numbered lists, links, inline images, blockquotes, code)."""
from survey_agent.server import render_question, _md_to_html
from survey_agent.engine import validate as v


def test_md_basic_formatting():
    html = _md_to_html("# How to\n\nDo **this** and *that*.\n\n- one\n- two\n\n1. first\n2. second\n\n"
                       "> note\n\n`code` and [link](https://x).")
    assert "<h2>How to</h2>" in html                       # # -> h2 (fits inside a card)
    assert "<strong>this</strong>" in html and "<em>that</em>" in html
    assert html.count("<ul>") == 1 and html.count("<li>") >= 4 and "<ol>" in html
    assert "<blockquote>" in html and "<code>code</code>" in html
    assert '<a href="https://x" target="_blank" rel="noopener">link</a>' in html


def test_md_inline_image():
    html = _md_to_html("Here:\n\n![a phone](https://x/y.png)")
    assert '<img src="https://x/y.png" alt="a phone"' in html


def test_md_is_safe():
    html = _md_to_html("<script>alert(1)</script>\n\n[x](javascript:alert(1))")
    assert "<script>" not in html and "&lt;script&gt;" in html        # escaped, not executed
    assert 'href="#"' in html and "javascript:" not in html           # bad URL scheme neutralized


def test_instructions_renders_a_formatted_body():
    html = render_question({"id": "howto", "type": "instructions", "title": "Task instructions",
                            "body": "## Steps\n- click the link\n- submit the code"}, {})
    assert "Task instructions" in html and "<h3>Steps</h3>" in html and "<li>click the link</li>" in html


def test_body_renders_as_description_on_a_question():
    html = render_question({"id": "q", "type": "radio", "title": "Pick", "body": "Choose the **best** one.",
                            "options": [{"value": "a", "label": "A"}]}, {})
    assert 'class="desc"' in html and "<strong>best</strong>" in html


def test_instructions_body_reaches_the_runtime_page_payload():
    # regression: the runtime projects questions into the page payload; `body` must survive (and pipe)
    from survey_agent.engine.runtime import SurveyRuntime
    spec = {"survey": {"id": "h", "title": "T", "settings": {"url_variables": ["uid", "rid"]},
            "pages": [{"id": "p", "questions": [
                {"id": "q_howto", "type": "instructions", "title": "How to {{url:group}}",
                 "body": "Step **1**\n- do it"}]}],
            "actions": [{"type": "complete"}]}}
    rt = SurveyRuntime(spec)
    step = rt.start(rt.new_session("t", {"uid": "1", "rid": "1", "group": "demo"}))
    q = step["page"]["questions"][0]
    assert q["id"] == "q_howto" and "Step **1**" in q["body"]   # body carried through the projection
    assert "demo" in q["title"]                                 # …and piped


def test_instructions_with_body_validates():
    # regression: `body` is now in the schema, so a how-to page passes additionalProperties:false
    spec = {"survey": {"id": "h", "title": "T", "pages": [{"id": "p", "questions": [
        {"id": "q_howto", "type": "instructions", "title": "How to",
         "body": "Do **this**.\n\n- step 1\n- step 2\n\n![diagram](https://x/d.png)"}]}],
        "actions": [{"type": "complete"}]}}
    assert [e for e in v.validate(spec) if not e.startswith("[warn]")] == []
