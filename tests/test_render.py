"""Renderer coverage for the question types that were previously 'partial' — date now uses a native
picker, ranking has working reorder controls, grid renders per-row cells, short text supports
email/maxlength. (The form renderer is the primary delivery surface, so these are the gates for
calling those types 'done'.)"""
from survey_agent.server import render_question


def r(q):
    return render_question(q, {})


def test_date_renders_native_picker_with_bounds():
    html = r({"id": "d", "type": "date", "title": "When?", "validation": {"min": "2020-01-01", "max": "2026-01-01"}})
    assert "type=date" in html and 'min="2020-01-01"' in html and 'max="2026-01-01"' in html


def test_short_text_email_and_maxlength():
    html = r({"id": "e", "type": "text", "title": "Email", "validation": {"format": "email", "max_length": 80}})
    assert "type=email" in html and 'maxlength="80"' in html


def test_ranking_has_reorder_controls_and_hidden_order():
    html = r({"id": "rk", "type": "ranking", "title": "Order these",
              "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}, {"value": "c", "label": "C"}]})
    assert "rmove(this,-1)" in html and "rmove(this,1)" in html
    assert html.count("type=hidden") == 3 and html.count("<li") == 3   # one orderable row per option


def test_grid_renders_a_cell_per_row_and_column():
    q = {"id": "g", "type": "grid", "title": "Rate", "cell_type": "radio",
         "options": [{"value": "1", "label": "Low"}, {"value": "2", "label": "High"}],
         "rows": [{"id": "r1", "label": "Speed"}, {"id": "r2", "label": "Price"}]}
    html = r(q)
    assert "g::r1" in html and "g::r2" in html      # cells namespaced by row
    assert html.count("type=radio") == 4            # 2 rows × 2 columns


def test_grid_checkbox_cells():
    q = {"id": "g", "type": "grid", "title": "Pick", "cell_type": "checkbox",
         "options": [{"value": "1", "label": "A"}], "rows": [{"id": "r1", "label": "Row"}]}
    assert "type=checkbox" in r(q)


# --- inline markdown in display text (titles + option/grid/ranking labels) ---

def test_title_renders_inline_markdown():
    html = r({"id": "q1", "type": "text", "title": "Pick your **top 3** and tick *Someone else*"})
    assert "<strong>top 3</strong>" in html and "<em>Someone else</em>" in html
    assert "**top 3**" not in html                                  # no literal asterisks leak

def test_title_renders_markdown_link():
    html = r({"id": "q1", "type": "text", "title": "See our [privacy policy](https://example.com/privacy)"})
    assert '<a href="https://example.com/privacy"' in html and ">privacy policy</a>" in html

def test_radio_and_checkbox_labels_render_markdown_but_value_stays_plain():
    q = {"id": "c", "type": "checkbox", "options": [
        {"value": "agree", "label": "I agree to the [terms](https://example.com/terms)"}]}
    html = r(q)
    assert '<a href="https://example.com/terms"' in html and ">terms</a>" in html
    assert 'value="agree"' in html                                  # the stored value is untouched (no markdown)

def test_dropdown_option_labels_stay_plain():
    # <option> can't render HTML — markdown must NOT be injected there (it would show as raw text)
    html = r({"id": "d", "type": "dropdown", "options": [{"value": "a", "label": "**A**"}]})
    assert "<strong>" not in html and "**A**" in html

def test_grid_labels_render_markdown():
    q = {"id": "g", "type": "grid", "cell_type": "radio",
         "options": [{"value": "1", "label": "**Low**"}], "rows": [{"id": "r1", "label": "*Speed*"}]}
    html = r(q)
    assert "<strong>Low</strong>" in html and "<em>Speed</em>" in html
