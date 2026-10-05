"""Survey templates — flat-file scaffolds for new surveys (a scaffold-time overlay seam).

A template is *data*, resolved only by `survey-agent new` — the engine, runtime, store and export
never see one (they only ever handle a normal `*.survey.json`). A template is either:

  • a directory  <id>/        with `template.yaml` (manifest) + `survey.json` (spec, ${param} tokens), or
  • a bare file  <id>.survey.json   (manifest inferred; the canonical params slug/title still apply).

Resolution layers, later overriding earlier *by id* (each carries `source`):

  1. core built-ins .... this package dir                       (brand-neutral reference templates)
  2. plugins .......... entry-point group `survey_agent.templates`
  3. overlay dirs ..... $SURVEY_TEMPLATES_DIR (os.pathsep-joined) + ./templates if present

A deployer's brand templates live in layer 2 or 3 — never in core. See docs/dev/TEMPLATES.md.
"""
import json
import os
import re

CORE_DIR = os.path.dirname(os.path.abspath(__file__))
_TOKEN = re.compile(r"\$\{([a-zA-Z0-9_]+)\}")   # only declared ${name} tokens are substituted


def _read(path):
    with open(path) as f:
        return f.read()


def _normalize(man, default_id, spec_path, source):
    return {
        "id": man.get("id") or default_id,
        "title": man.get("title") or default_id,
        "description": man.get("description") or "",
        "type": man.get("type") or "custom",
        "tags": man.get("tags") or [],
        "theme": man.get("theme"),
        "params": man.get("params") or [],
        "requires": man.get("requires") or {},
        "spec_path": spec_path,
        "source": source,
    }


def _from_dir(name, path, source):
    man_path = os.path.join(path, "template.yaml")
    spec_path = os.path.join(path, "survey.json")
    if not (os.path.exists(man_path) and os.path.exists(spec_path)):
        return None
    try:
        import yaml
        return _normalize(yaml.safe_load(_read(man_path)) or {}, name, spec_path, source)
    except Exception:
        return None       # a malformed template must not break discovery of the others


def _from_file(stem, path, source):
    """Bare <stem>.survey.json — infer a minimal manifest from the spec itself."""
    try:
        s = (json.loads(_read(path)) or {}).get("survey", {})
    except Exception:
        return None       # a malformed template must not break discovery of the others
    man = {
        "id": stem,
        "title": s.get("title") or stem,
        "description": (s.get("description") or "").split("\n")[0],
        "type": (s.get("tags") or ["custom"])[0],
        "tags": s.get("tags") or [],
        "theme": (s.get("settings") or {}).get("theme"),
        "params": [],
    }
    return _normalize(man, stem, path, source)


def _scan(root, source):
    out = {}
    if not root or not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        t = None
        if os.path.isdir(p):
            t = _from_dir(name, p, source)
        elif name.endswith(".survey.json"):
            t = _from_file(name[: -len(".survey.json")], p, source)
        if t:
            out[t["id"]] = t
    return out


def _plugin_dirs():
    try:
        from importlib.metadata import entry_points
    except Exception:
        return []
    dirs = []
    try:
        for ep in entry_points(group="survey_agent.templates"):
            try:
                obj = ep.load()
                d = obj() if callable(obj) else obj
                if isinstance(d, str) and os.path.isdir(d):
                    dirs.append(d)
            except Exception:
                pass
    except Exception:
        pass
    return dirs


def _overlay_dirs():
    """Overlay template dirs, lowest precedence first (discover() applies them in order, later
    winning by id): the conventional ./overlay/templates and ./templates, then an explicit
    $SURVEY_TEMPLATES_DIR — so an env override wins. Mirrors themes' ./overlay/themes convention."""
    dirs = [d for d in (os.path.join("overlay", "templates"), "templates") if os.path.isdir(d)]
    env = os.environ.get("SURVEY_TEMPLATES_DIR")
    if env:
        dirs += [d for d in env.split(os.pathsep) if d and os.path.isdir(d)]
    return dirs


def discover():
    """id -> template dict, later layers winning. `source` ∈ {core, plugin, overlay}."""
    out = {}
    out.update(_scan(CORE_DIR, "core"))
    for d in _plugin_dirs():
        out.update(_scan(d, "plugin"))
    for d in _overlay_dirs():
        out.update(_scan(d, "overlay"))
    return out


def list_templates(type=None):
    ts = sorted(discover().values(), key=lambda t: (t["type"], t["id"]))
    return [t for t in ts if not type or t["type"] == type]


def get(template_id):
    return discover().get(template_id)


def _values(template, provided):
    """Provided params over declared defaults; resolve ${param} refs inside default values."""
    vals = dict(provided or {})
    for p in template.get("params", []):
        name = p.get("name")
        if name and name not in vals and "default" in p:
            vals[name] = p["default"]
    for _ in range(3):                      # resolve nested ${param} refs in values
        changed = False
        for k, v in list(vals.items()):
            if isinstance(v, str) and "${" in v:
                nv = _TOKEN.sub(lambda m: str(vals.get(m.group(1), m.group(0))), v)
                if nv != v:
                    vals[k], changed = nv, True
        if not changed:
            break
    return vals


def missing_params(template, provided):
    vals = _values(template, provided)
    return [p.get("name") for p in template.get("params", [])
            if p.get("required") and not vals.get(p.get("name"))]


def _escape(v):
    """Escape a value for safe insertion into a JSON string literal — every ${token} sits inside a
    JSON string, so a value with a quote/backslash/newline can't corrupt the document."""
    return json.dumps(str(v))[1:-1]


def render(template, params):
    """Return a substituted, ready-to-validate survey spec dict.

    Raises ValueError if a ${token} is left unresolved (an undeclared/unprovided param) or if the
    substituted text isn't valid JSON — so `survey-agent new` never silently writes a broken survey."""
    vals = _values(template, params)
    text = _TOKEN.sub(lambda m: _escape(vals[m.group(1)]) if m.group(1) in vals else m.group(0),
                      _read(template["spec_path"]))
    unresolved = sorted(set(_TOKEN.findall(text)))
    if unresolved:
        raise ValueError(f"template '{template['id']}': unresolved ${{…}} token(s): "
                         + ", ".join(unresolved) + " — declare them in template.yaml params "
                         "(with a default, or required) or pass --param name=value")
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"template '{template['id']}': substitution produced invalid JSON ({e})") from e
    s = spec.get("survey", spec)
    if vals.get("slug"):
        s["id"] = vals["slug"]
    if vals.get("title"):
        s["title"] = vals["title"]
    req = template.get("requires") or {}
    if req.get("url_variables"):            # merge required url vars into settings (de-duped)
        uv = s.setdefault("settings", {}).setdefault("url_variables", [])
        for v in req["url_variables"]:
            if v not in uv:
                uv.append(v)
    return spec


def from_survey(spec, id=None, title=None, type=None):
    """Promote an existing survey spec into a template — the inverse of `render`, shared by
    `survey-agent templatize` and the MCP `templatize_survey` tool (pure; no I/O).

    Returns `(manifest, spec)`: a `template.yaml` dict and the SAME spec object mutated so its `id`/
    `title` become the canonical `${slug}`/`${title}` tokens and its `version` resets to 1 (scaffolds
    start a fresh lineage). Theme + url_variables are lifted from the survey's settings into the
    manifest (params/requires). Any OTHER knob is a one-line hand-edit to the manifest afterward —
    this does the deterministic part only."""
    s = spec.get("survey", spec)
    settings = s.get("settings") or {}
    tid = id or s.get("id") or "template"
    manifest = {
        "id": tid,
        "title": title or s.get("title") or tid,
        "type": type or (s.get("tags") or ["custom"])[0],
        "description": (s.get("description") or "").split("\n")[0]
                       or f"Template scaffolded from the '{s.get('id', tid)}' survey.",
        "params": [
            {"name": "slug", "prompt": "Survey id / slug", "required": True},
            {"name": "title", "prompt": "Survey title", "required": True},
        ],
    }
    if settings.get("theme"):
        manifest["theme"] = settings["theme"]
    if settings.get("url_variables"):
        manifest["requires"] = {"url_variables": list(settings["url_variables"])}
    s["id"], s["title"], s["version"] = "${slug}", "${title}", 1   # the two tokens + fresh version
    return manifest, spec


def verify_dir(template_id, params_dict, requires, spec_path):
    """Render a freshly-written template dir + return its substituted spec, so a writer can validate it
    round-trips. `params_dict`/`requires` are the manifest's; `spec_path` is the written survey.json."""
    probe = {"id": template_id, "params": params_dict, "requires": requires or {}, "spec_path": spec_path}
    return render(probe, {"slug": "_verify", "title": "Verify"})
