from survey_agent import themes


def test_builtin_themes_load():
    names = themes.list_themes()
    assert "paper" in names and "dark" in names
    css = themes.load_css("paper")
    assert ":root{" in css and "--ms-" in css
    assert ".q .title" in css                       # base.css is always included


def test_unknown_theme_falls_back_to_default():
    assert themes.load_css("does-not-exist") == themes.load_css(themes.DEFAULT_THEME)


def test_external_theme_dir_is_discovered_and_appends_overrides(tmp_path, monkeypatch):
    ext = tmp_path / "themes"
    (ext / "brandx").mkdir(parents=True)
    (ext / "brandx" / "tokens.json").write_text('{"primary": "#abcdef", "_note": "skip me"}')
    (ext / "brandx" / "overrides.css").write_text(".brandx{color:red}")
    monkeypatch.setenv("SURVEY_THEMES_DIR", str(ext))

    assert "brandx" in themes.list_themes()
    css = themes.load_css("brandx")
    assert "--ms-primary:#abcdef;" in css           # tokens become :root custom properties
    assert "--ms-_note" not in css                  # underscore-prefixed keys are skipped
    assert ".q .title" in css                       # base.css still loads from the package
    assert ".brandx{color:red}" in css              # the theme's overrides.css is appended


def test_external_theme_overrides_builtin_by_name(tmp_path, monkeypatch):
    ext = tmp_path / "themes"
    (ext / "paper").mkdir(parents=True)
    (ext / "paper" / "tokens.json").write_text('{"primary": "#000000"}')
    monkeypatch.setenv("SURVEY_THEMES_DIR", str(ext))
    assert "--ms-primary:#000000;" in themes.load_css("paper")   # overlay wins over the bundled paper
