"""Operator console glue — the read-first API onto the engine (board, detail, status, templates)."""
import json


SPEC = {"survey": {"id": "c1", "version": 1, "title": "Console One", "tags": ["demo"],
                   "settings": {"url_variables": ["uid"]},
                   "pages": [{"id": "p1", "questions": [
                       {"id": "q1", "type": "radio", "required": True, "title": "Use it?",
                        "options": [{"value": "y", "label": "Yes"},
                                    {"value": "n", "label": "No", "exclusive": True}]}]}],
                   "actions": [{"type": "disqualify", "when": {"ref": {"kind": "question", "id": "q1"}, "operator": "equals", "value": "n"}},
                               {"type": "complete", "redirect": {"kind": "default"}}]}}


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SURVEY_DIR", str(tmp_path))
    monkeypatch.setenv("SURVEY_DEFINITION_SOURCE", "file")
    monkeypatch.setenv("SURVEY_STORE", "memory")
    (tmp_path / "c1.survey.json").write_text(json.dumps(SPEC))


def test_overview_lists_the_survey(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from survey_agent import console
    ov = console.overview()
    assert ov["vitals"]["surveys"] == 1
    row = ov["surveys"][0]
    assert row["id"] == "c1" and row["title"] == "Console One"
    assert row["completes"] == 0 and "incidence" in row and row["url"].endswith("/s/c1")


def test_detail_previews_pages_logic_and_terminals(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from survey_agent import console
    d = console.survey_detail("c1")
    assert d["pages"][0]["questions"][0]["type"] == "radio"
    assert d["pages"][0]["questions"][0]["options"][1]["exclusive"] is True   # exclusive surfaced
    assert sorted(a["type"] for a in d["actions"]) == ["complete", "disqualify"]
    assert d["url_variables"] == ["uid"]
    assert console.survey_detail("nope") is None


def test_set_status_round_trips(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from survey_agent import console
    assert console.set_status("c1", "paused")["state"] == "paused"
    assert console.survey_detail("c1")["state"] == "paused"
    import pytest
    with pytest.raises(ValueError):
        console.set_status("c1", "bogus")


def test_template_catalog_nonempty(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from survey_agent import console
    ids = {t["id"] for t in console.template_catalog()}
    assert "blank" in ids            # the core templates are discovered


def test_create_from_template_scaffolds_a_draft(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from survey_agent import console
    r = console.create_from_template("blank", "My New Survey!", "My New Survey")
    assert r["ok"] and r["id"] == "my-new-survey"          # id slugified
    d = console.survey_detail("my-new-survey")
    assert d["state"] == "draft" and d["title"] == "My New Survey"
    assert "my-new-survey" in {s["id"] for s in console.overview()["surveys"]}
    import pytest
    with pytest.raises(ValueError):                        # no duplicate ids
        console.create_from_template("blank", "my-new-survey")
    with pytest.raises(ValueError):                        # id required
        console.create_from_template("blank", "")


def test_copy_survey_duplicates_into_a_fresh_draft(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from survey_agent import console
    console.set_status("c1", "live")                       # source is live...
    r = console.copy_survey("c1", "Console One Feb", "Console One Feb")
    assert r["ok"] and r["id"] == "console-one-feb"        # id slugified from the new name
    d = console.survey_detail("console-one-feb")
    assert d["state"] == "draft"                           # ...but the copy is a clean draft
    assert d["title"] == "Console One Feb" and d["version"] == 1
    assert d["pages"] == console.survey_detail("c1")["pages"]   # content carried verbatim
    assert console.survey_detail("c1")["state"] == "live"      # source untouched
    import pytest
    with pytest.raises(ValueError):                        # unknown source
        console.copy_survey("nope", "x")
    with pytest.raises(ValueError):                        # new id required
        console.copy_survey("c1", "")
    with pytest.raises(ValueError):                        # no duplicate ids
        console.copy_survey("c1", "console-one-feb")


def test_delete_survey(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from survey_agent import console
    console.create_from_template("blank", "gone", "Gone")
    assert console.delete_survey("gone")["ok"]
    assert console.survey_detail("gone") is None
    assert "gone" not in {s["id"] for s in console.overview()["surveys"]}
    import pytest
    with pytest.raises(ValueError):
        console.delete_survey("nope")


def test_validate_then_simulate_advance_the_lifecycle(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from survey_agent import console
    v = console.validate_survey("c1")
    assert v["ok"] and v["errors"] == [] and v["state"] == "validated"     # clean validate bumps draft→validated
    s = console.simulate_survey("c1", n=60)
    assert s["ok"] and s["problems"] == [] and s["state"] == "tested"      # clean coverage bumps →tested
    assert s["n"] == 60 and "completed" in s["statuses"]
    assert console.validate_survey("nope") is None and console.simulate_survey("nope") is None


def test_insights_html_renders_readout(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("SURVEY_LLM", "stub")                 # offline-deterministic engine
    from survey_agent import console
    h = console.insights_html("c1", simulate_n=30)           # simulate so there's something to analyse
    assert h and h.lstrip().startswith("<") and "AI read-out" in h and "stub" in h
    assert console.insights_html("nope") is None


def test_store_is_memoized_not_rebuilt_per_call(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from survey_agent import console
    assert console._respstore() is console._respstore()       # one instance (a board no longer reconnects per survey)
    assert console._defstore() is console._defstore()


def test_overview_counts_via_one_grouped_call(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("SURVEY_STORE", "file")                # file store has count_responses_by_survey
    monkeypatch.setenv("SURVEY_DATA_DIR", str(tmp_path / "data"))
    from survey_agent import console
    st = console._respstore()
    assert hasattr(st, "count_responses_by_survey")
    st.record_response("c1", {"status": "Complete"})
    st.record_response("c1", {"status": "Disqualified"})
    grouped = st.count_responses_by_survey()
    assert grouped["c1"] == {"complete": 1, "disqualified": 1, "total": 2}   # single bucketed pass
    row = {r["id"]: r for r in console.overview()["surveys"]}["c1"]
    assert row["completes"] == 1 and row["screenouts"] == 1 and row["incidence"] == 0.5


def test_project_grouping_is_mutable_metadata(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from survey_agent import console
    before = console.survey_detail("c1")["version"]
    d = console.set_project("c1", "  Northwind  ")               # trimmed; stored in the registry, not the def
    assert d["project"] == "Northwind" and d["version"] == before  # moving must NOT bump the version
    assert {s["id"]: s["project"] for s in console.overview()["surveys"]}["c1"] == "Northwind"
    assert console.set_project("c1", "")["project"] is None     # "" → ungrouped
    import pytest
    with pytest.raises(ValueError):
        console.set_project("nope", "X")


def test_mark_live_pins_the_version(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from survey_agent import console
    d = console.set_status("c1", "live")
    assert d["state"] == "live"
    reg = console._defstore().get_registry("c1")
    assert reg["live_version"] == 1 and (reg.get("live") or {}).get("at")   # launched version pinned + stamped


def test_preview_url_carries_token_when_set(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from survey_agent import console
    assert "preview=" not in console.survey_detail("c1")["preview_url"]     # no token configured → plain preview link
    monkeypatch.setenv("SURVEY_PREVIEW_TOKEN", "abc123")
    pv = console.survey_detail("c1")["preview_url"]
    assert pv.endswith("preview=abc123") and "/s/c1?" in pv                 # token appended so it clears the gate


def test_preview_url_uses_configured_identity_vars(tmp_path, monkeypatch):
    # The 'open as respondent' link must carry the survey's *own* identity vars (e.g. pid/tid), not
    # the engine defaults — else a walk-through (and its terminal redirect's id passback) carries none.
    import urllib.parse
    spec = json.loads(json.dumps(SPEC))
    spec["survey"]["settings"] = {"url_variables": ["pid", "tid"],
                                  "identity": {"respondent": "pid", "response": "tid"}}
    monkeypatch.setenv("SURVEY_DIR", str(tmp_path))
    monkeypatch.setenv("SURVEY_DEFINITION_SOURCE", "file")
    monkeypatch.setenv("SURVEY_STORE", "memory")
    (tmp_path / "c1.survey.json").write_text(json.dumps(spec))
    from survey_agent import console
    pv = console.survey_detail("c1")["preview_url"]
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(pv).query))
    assert q.get("pid") == "preview" and q.get("tid") == "preview-1"      # the survey's identity vars
    assert "uid" not in q and "rid" not in q                               # not the engine defaults


def test_create_from_template_defaults_title_and_fills_placeholders(tmp_path, monkeypatch):
    # No title supplied → derive a humanized default from the id; ${slug}/${title} placeholders must
    # not leak anywhere (incl. nested page/question titles).
    _setup(tmp_path, monkeypatch)
    from survey_agent import console
    console.create_from_template("explainer", "product-feedback-q1")       # no title arg
    d = console.survey_detail("product-feedback-q1")
    assert d["title"] == "Product Feedback Q1"                             # humanized from the id
    assert "${" not in json.dumps(d)                                       # no placeholder leak


def test_runtime_status_reports_reachability(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("SURVEY_PUBLIC_BASE_URL", "http://127.0.0.1:8771")   # nothing serving here
    from survey_agent import console
    rs = console.runtime_status()
    assert rs["host"] == "127.0.0.1" and rs["port"] == 8771 and rs["local"] is True and rs["up"] is False


def test_health_reports_config(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from survey_agent import console
    h = console.health()
    assert h["definition_source"] == "file" and h["response_store"] == "memory"
    assert h["output_dir"] and "surveys" in h
    assert "runtime" in h and "up" in h["runtime"]                          # runtime reachability surfaced


def test_help_renders_html():
    from survey_agent import console
    html = console.help_html()
    assert "<" in html and ("Board" in html or "operator" in html.lower())


def test_dashboard_html_lists_surveys(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    from survey_agent import console
    html = console.dashboard_html()
    assert html.lstrip().startswith("<") and "c1" in html      # the all-surveys management dashboard


def test_index_html_is_self_contained():
    from survey_agent import console
    html = console.INDEX_HTML
    assert html.startswith("<!doctype html>") and "operator console" in html
    assert "//cdn" not in html                              # no external CDN deps
    assert all(x in html for x in ["data-v=help", "id=tools", "id=theme", "id=stamp", "applyTheme"])
    assert all(x in html for x in ["id=val", "id=sim", "id=ins", "chkmount", "insmount", "runtimeNote", "@keyframes throb"])
    assert all(x in html for x in ["id=uv", "uvUrl", "include captured params"])   # copyable URL ± captured params
    assert all(x in html for x in ["pgroup", "phead", "projbar", "/project"])       # collapsible project groups + move


def test_console_brand_is_configurable(monkeypatch):
    from survey_agent import console
    # default keeps the open-core brand (so upstream syncs don't conflict), in both the tab title + rail
    html = console._render_index()
    assert "<title>survey-agent · console</title>" in html
    assert "survey-agent<small>operator console</small>" in html
    # a deployment overrides it via SURVEY_CONSOLE_BRAND
    monkeypatch.setenv("SURVEY_CONSOLE_BRAND", "my-team-surveys")
    html = console._render_index()
    assert "<title>my-team-surveys · console</title>" in html
    assert "my-team-surveys<small>operator console</small>" in html
    assert "survey-agent · console" not in html and "survey-agent<small>" not in html


def test_console_footer_shows_real_host_not_hardcoded(monkeypatch):
    from survey_agent import console
    # remote base → footer shows the actual host, never the old hardcoded "local · 127.0.0.1"
    monkeypatch.setenv("SURVEY_PUBLIC_BASE_URL", "https://surveys.example.com")
    html = console._render_index()
    assert "<div>surveys.example.com</div>" in html
    assert "local · 127.0.0.1" not in html
    # local base → labelled as local with the real host
    monkeypatch.setenv("SURVEY_PUBLIC_BASE_URL", "http://127.0.0.1:8000")
    assert "<div>local · 127.0.0.1</div>" in console._render_index()


def test_filter_rerenders_results_only_so_input_keeps_focus():
    # the focus bug: renderBoard() rebuilt the whole #wrap (incl. the #q input) on every keystroke,
    # so the input was destroyed/recreated and lost focus. The input now lives in a once-rendered
    # toolbar; only a separate #results container re-renders on filter.
    from survey_agent import console
    html = console.INDEX_HTML
    assert "<div id=results></div>" in html                                  # dedicated results container
    assert "renderRows()}" in html and "function renderRows()" in html       # filter calls renderRows…
    assert "$('#results').innerHTML=body" in html                            # …which only touches #results
    assert "renderBoard();q.focus()" not in html                             # old per-keystroke teardown gone
