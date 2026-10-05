"""Survey theming — flat-file themes (design tokens + shared structural CSS).

A theme is a directory with `tokens.json` (a flat map of design tokens) and an optional
`overrides.css`. All themes share `base.css`, which styles the survey using CSS custom
properties (`var(--ms-bg)`, etc.). `load_css(name)` returns `:root{…tokens…} + base.css +
overrides.css`. A survey picks its theme via `settings.theme`; the runtime can also override
per-request (e.g. `?theme=dark`) for preview.

Themes can also live OUTSIDE the package — in `$SURVEY_THEMES_DIR` (os.pathsep-separated) or a
conventional `./overlay/themes` (CWD-relative, like the `SURVEY_DIR`/`SURVEY_ASSETS_DIR` defaults).
External theme dirs are searched BEFORE the built-ins, so a deployer's brand theme can live in a
private overlay (kept out of the open core) and even override a bundled theme by name. `base.css`
always comes from the package.

Themes are files so they're version-controlled and Claude-authorable like everything else.
"""
import json
import os

THEMES_DIR = os.path.dirname(os.path.abspath(__file__))   # built-in themes + the shared base.css
DEFAULT_THEME = "paper"


def _extra_theme_dirs():
    """Deployer/brand theme directories outside the open core, highest-precedence first."""
    dirs = [d for d in os.environ.get("SURVEY_THEMES_DIR", "").split(os.pathsep) if d]
    dirs.append(os.path.join("overlay", "themes"))        # conventional, CWD-relative
    return [d for d in dirs if os.path.isdir(d)]


def _search_dirs():
    return _extra_theme_dirs() + [THEMES_DIR]             # external overlays win over built-ins


def _theme_dir(name):
    """The directory holding `name`'s tokens.json, or None."""
    if not name:
        return None
    for base in _search_dirs():
        if os.path.exists(os.path.join(base, name, "tokens.json")):
            return os.path.join(base, name)
    return None


def list_themes():
    found = set()
    for base in _search_dirs():
        for d in os.listdir(base):
            if os.path.exists(os.path.join(base, d, "tokens.json")):
                found.add(d)
    return sorted(found)


def _exists(name):
    return _theme_dir(name) is not None


def tokens(name):
    with open(os.path.join(_theme_dir(name), "tokens.json")) as f:
        return json.load(f)


def _read(path):
    with open(path) as f:
        return f.read()


def load_css(name=None):
    name = name if _exists(name) else DEFAULT_THEME
    tdir = _theme_dir(name)
    root = ":root{" + "".join(f"--ms-{k}:{v};" for k, v in tokens(name).items()
                                if not k.startswith("_")) + "}"
    css = root + "\n" + _read(os.path.join(THEMES_DIR, "base.css"))   # base.css always from the package
    ov = os.path.join(tdir, "overrides.css")
    if os.path.exists(ov):
        css += "\n" + _read(ov)
    return css
