"""Image stimulus — a display-only `image` type that points at a URL, with sanitized size controls.
(Video already existed; this mirrors it.)"""
from survey_agent.server import render_question
from survey_agent.engine import validate as v
from survey_agent import simulate


def _img(media):
    return render_question({"id": "i", "type": "image", "title": "Look", "media": media}, {})


def test_image_renders_from_url_with_alt_and_caption():
    html = _img({"url": "https://x/y.png", "caption": "Concept A", "alt": "a phone"})
    assert "<img" in html and 'src="https://x/y.png"' in html
    assert 'alt="a phone"' in html and "Concept A" in html and 'loading="lazy"' in html
    assert "max-width:100%" in html                 # responsive default, never overflows


def test_image_size_controls_applied_and_sanitized():
    ok = _img({"url": "u", "max_width": "480px", "width": "300px"})
    assert "max-width:480px" in ok and "width:300px" in ok
    bad = _img({"url": "u", "max_width": "100px;background:url(evil)"})   # style-injection attempt
    assert "background" not in bad and "max-width:100%" in bad           # rejected -> safe default


def test_image_requires_media_url():
    spec = {"survey": {"id": "img", "title": "T", "pages": [
        {"id": "p", "questions": [{"id": "q_img", "type": "image", "title": "Look"}]},
        {"id": "p2", "questions": [{"id": "q", "type": "radio", "title": "ok?", "options": [{"value": "y", "label": "Y"}]}]}],
        "actions": [{"type": "complete"}]}}
    assert any("media.url" in e for e in v.validate(spec))
    spec["survey"]["pages"][0]["questions"][0]["media"] = {"url": "https://x/y.png"}
    assert [e for e in v.validate(spec) if not e.startswith("[warn]")] == []


def test_image_is_display_only_in_simulation():
    spec = {"survey": {"id": "img2", "title": "T", "settings": {"url_variables": ["uid", "rid"]},
            "pages": [{"id": "p1", "questions": [
                {"id": "q_img", "type": "image", "title": "Concept", "media": {"url": "https://x/y.png"}},
                {"id": "q_use", "type": "radio", "required": True, "title": "Use it?",
                 "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}]}]}],
            "actions": [{"type": "disqualify", "reason_code": "no_use",
                         "when": {"ref": {"kind": "question", "id": "q_use"}, "operator": "equals", "value": "no"}},
                        {"type": "complete"}]}}
    cov = simulate.coverage(spec, n=30)
    assert cov["problems"] == []                     # the image needs no answer; both paths reachable
