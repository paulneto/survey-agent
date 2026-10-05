"""survey-agent-mcp — the connector-grade agentic interface.

This is the universal adapter: the same server backs Claude Code, a Claude Desktop extension, a
claude.ai remote connector, and the Agent SDK. It exposes all three MCP primitives:

  • tools     — validate / critique / compile / simulate / pretest / save / new / publish / launch +
                report / insights / export / dashboard / list / set_status + `capabilities`, plus
                **begin_survey / answer_survey**: run a logic-gated survey inline to collect clean
                structured data from a human (survey-as-a-tool for any agent). The connecting agent IS
                the author — it drafts a spec, then save_survey → validate → simulate (the closed loop).
  • resources — survey catalog, the JSON schema, themes, and individual surveys (so an agent can
                introspect what exists instead of guessing)
  • prompts   — the authoring workflow (brief → draft → validate → simulate → approve → launch)

Every tool runs the pure engine + the configured seams (definitions / store) locally — no separate
service required. With a runtime backend (SURVEY_DEFINITION_SOURCE/SURVEY_STORE=mongo, or =http to go
through the API gateway) publish/report read and write through that seam.

Transports (console script `survey-agent-mcp`, or `python -m survey_agent.mcp.server`):
  survey-agent-mcp                         # stdio  (Claude Code / Claude Desktop — zero infra)
  survey-agent-mcp --transport sse         # remote (claude.ai Connector; put OAuth in front)

Safety: `launch_survey` is gated SERVER-SIDE by an approval token (SURVEY_LAUNCH_APPROVAL_TOKEN) —
an autonomous agent cannot field a survey or spend budget on its own. Simulate is the default.

Requires the `mcp` package; if absent it prints guidance and exits.
"""
import argparse
import hmac
import json
import os
import sys

from survey_agent import load_env_files

# The MCP server is often spawned (by Claude Code / Desktop) from a directory that ISN'T the project,
# so — unlike the CLI — it can't rely on a cwd `.env`. Load it up front from $SURVEY_ENV_FILE, ./.env,
# or the .env next to $SURVEY_DIR, so AI-backed tools see ANTHROPIC_API_KEY without anyone exporting it
# by hand (otherwise they silently fall back to the offline stub). setdefault: a real env var wins.
load_env_files()

SURVEYS_DIR = os.environ.get("SURVEY_DIR", "surveys")   # user's survey files (cwd-relative)
from survey_agent.engine.validate import SCHEMA_PATH       # the packaged schema

try:
    from mcp.server.fastmcp import FastMCP
except Exception:  # pragma: no cover
    print("The 'mcp' package is required: pip install 'survey-agent[mcp]'\n"
          "Then add it to your client, e.g.: claude mcp add survey-agent -- "
          "uvx --from 'survey-agent[mcp,llm]' survey-agent-mcp", file=sys.stderr)
    raise SystemExit(1)

from survey_agent.engine import validate as validate_mod          # noqa: E402  (pure engine — always available)
from survey_agent.engine.compile_md import compile_markdown        # noqa: E402

app = FastMCP("survey-agent")
# serverInfo.version: report survey-agent's version, not the MCP SDK's (FastMCP leaves it unset, and
# the low-level server then falls back to the `mcp` package version).
from survey_agent import __version__ as _SA_VERSION  # noqa: E402
try:
    app._mcp_server.version = _SA_VERSION
except Exception:  # pragma: no cover — SDK internals moved; serverInfo falls back to the SDK version
    pass


# ----------------------------------------------------------------- helpers
def _surveys():
    out = []
    if os.path.isdir(SURVEYS_DIR):
        for name in sorted(os.listdir(SURVEYS_DIR)):
            if name.endswith(".survey.json"):
                try:
                    s = json.load(open(os.path.join(SURVEYS_DIR, name)))["survey"]
                    out.append({"survey_id": s["id"], "title": s.get("title"),
                                "version": s.get("version", 1), "file": name})
                except Exception:
                    continue
    return out


def _launch_approved(token):
    """The shared launch gate (survey_agent.config.may_mark_live) as seen by the connector: a matching
    SURVEY_LAUNCH_APPROVAL_TOKEN is required, and with none configured marking live is impossible."""
    from survey_agent import config
    return isinstance(token, str) and config.may_mark_live(token, surface="mcp")[0]


# survey-as-a-tool: in-process conversation sessions an agent drives (begin_survey/answer_survey).
# Bounded + LRU-evicted so a long-running connector doesn't accumulate conversations an agent began
# and never finished. Sessions are process-local — a multi-worker remote connector must pin them
# (sticky routing) or swap in a shared store. See docs/PRIMITIVE.md and mcp/sessions.py.
from survey_agent.mcp.sessions import BoundedSessionStore
_CONVOS = BoundedSessionStore(int(os.environ.get("SURVEY_MCP_MAX_SESSIONS", "256")))


def _load_survey_spec(survey):
    """Resolve `survey` (a survey_id from survey://catalog, or a path to a .survey.json) to a spec."""
    if survey.endswith(".json") or os.path.sep in survey:
        with open(survey) as f:
            return json.load(f)
    for s in _surveys():
        if s["survey_id"] == survey:
            with open(os.path.join(SURVEYS_DIR, s["file"])) as f:
                return json.load(f)
    raise ValueError(f"survey '{survey}' not found — pass a survey_id from survey://catalog or a path")


# ----------------------------------------------------------------- tools
@app.tool()
def capabilities() -> dict:
    """What this deployment can do — backends available, what's active, and whether launching is
    gated. Call this first so you don't assume a store/source/integration that isn't installed."""
    import survey_agent.themes as themes
    import survey_agent.templates as templates_mod
    from survey_agent.integrations import _plugins
    return {
        "stores": ["memory", "file", "sqlite", "mongo", "http", "<import-path>"],
        "context_sources": ["static", "url_var", "db.events", "db.query", "http", "prior_response", "geoip"],
        "persona_sources": ["synthetic", "<import-path>"],
        "integrations": ["none", "redirect", "webhook", "mailchimp"] + sorted(_plugins().keys()),
        "themes": themes.list_themes(),
        "templates": [t["id"] for t in templates_mod.list_templates()],
        "active": {"store": os.environ.get("SURVEY_STORE") or "memory",
                   "enrich": os.environ.get("SURVEY_ENRICH", "static")},
        # NOT a boolean "is launching gated" — that reads backwards. When no approval token is
        # configured, marking live is IMPOSSIBLE via this connector (fail-closed), not permitted.
        "launch": {
            "can_mark_live": bool(os.environ.get("SURVEY_LAUNCH_APPROVAL_TOKEN")),
            "approval_token_configured": bool(os.environ.get("SURVEY_LAUNCH_APPROVAL_TOKEN")),
            "requires": ["approval_token", "acknowledge_responsibilities=true"],
            "note": "launch_survey ALWAYS returns the survey_url. Marking a survey LIVE needs BOTH a "
                    "matching approval_token (from SURVEY_LAUNCH_APPROVAL_TOKEN) AND "
                    "acknowledge_responsibilities=true. When no token is configured, can_mark_live is "
                    "false and marking live is IMPOSSIBLE — not ungated. Fielding is a human act.",
        },
        "note": "Built-ins shown. Storage is a seam: SURVEY_STORE can also be an import path "
                "('my_pkg.store:YourStore'). context_sources/integrations grow via the plugin registry.",
    }


@app.tool()
def deployment_readiness() -> dict:
    """Is this deployment safe to field? Runs the same checks the runtime runs at startup and
    `survey-agent doctor` reports: dev-placeholder signing secrets, a wrong/missing store, the
    ephemeral-disk data-loss trap, the in-memory store. Returns {ready, checks:[{level, message}]};
    `ready` is false if any check is fatal. Does not open a live store connection, so it's safe to
    call anywhere. Call this before launching to real people."""
    from survey_agent import preflight
    checks = preflight.runtime_preflight(ping=False)
    return {"ready": not any(lvl == "fatal" for lvl, _ in checks),
            "checks": [{"level": lvl, "message": m} for lvl, m in checks]}


@app.tool()
def validate_survey(survey_json: dict) -> dict:
    """Validate a Survey spec (JSON schema + semantic lint). Returns {ok, errors[]}. Always run
    this before publish/launch."""
    errors = validate_mod.validate(survey_json)
    real = [e for e in errors if not e.startswith("[warn]")]
    return {"ok": not real, "errors": errors}


@app.tool()
def compile_markdown_survey(markdown: str) -> dict:
    """Compile an authorable Markdown survey into the canonical {survey:{...}} JSON."""
    return compile_markdown(markdown)


@app.tool()
def critique_survey(survey_json: dict) -> dict:
    """Methodology review of a Survey spec — flags leading/double-barreled questions, biased scales,
    missing neutral/opt-out options, overlapping numeric ranges, and jargon. Advisory (judgment, not
    pass/fail); run it before launch and address the high-severity items. Returns {issues:[{id,
    severity,type,issue,suggestion}], summary, engine}. Sees the definition only, never respondent data."""
    import survey_agent.ai as ai
    return ai.get_llm().critique(survey_json)


@app.tool()
def save_survey(survey_json: dict, slug: str = "") -> dict:
    """Persist a drafted Survey spec to SURVEY_DIR/<slug>.survey.json so the file-based tools
    (simulate_survey, begin_survey, list_surveys, the catalog) and the runtime can use it. Validates
    first and REFUSES to write an invalid spec. `slug` defaults to the survey id. Returns
    {ok, saved, path, slug, errors}. This is how an agent on a file-access surface (Claude Code,
    Cowork) turns an in-context draft into a managed survey file — so the coverage gate can run."""
    errors = validate_mod.validate(survey_json)
    real = [e for e in errors if not e.startswith("[warn]")]
    if real:
        return {"ok": False, "saved": False, "errors": errors,
                "message": "not saved — fix the validation errors first, then save again"}
    s = survey_json.get("survey", survey_json) if isinstance(survey_json, dict) else {}
    sid = slug or s.get("id")
    if not sid:
        return {"ok": False, "saved": False, "errors": ["survey has no `id` and no `slug` was given"]}
    os.makedirs(SURVEYS_DIR, exist_ok=True)
    path = os.path.join(SURVEYS_DIR, f"{sid}.survey.json")
    with open(path, "w") as f:
        json.dump(survey_json, f, indent=2)
        f.write("\n")
    # also write the readable, logic-annotated Markdown so the human has a reviewable version
    md_path = os.path.join(SURVEYS_DIR, f"{sid}.md")
    try:
        from survey_agent.engine.compile_md import render_markdown
        with open(md_path, "w") as f:
            f.write(render_markdown(survey_json))
    except Exception:
        md_path = None
    try:
        from survey_agent import registry
        registry.record_validation(s.get("id", sid), ok=True, sdir=SURVEYS_DIR)
    except Exception:
        pass
    return {"ok": True, "saved": True, "path": path, "md_path": md_path, "slug": sid, "errors": errors}


@app.tool()
def list_templates(type: str = "") -> list:
    """Survey scaffolds available to `new_survey`, across all layers — core reference templates,
    plugins, and the deployer's overlay. Each: {id, title, type, source(core|plugin|overlay),
    description, theme, params:[{name, required, default}]}. `type` filters by category (e.g.
    'screener', 'explainer', 'data-download'). Call this, pick an id, then `new_survey`."""
    from survey_agent import templates as T
    return [{"id": t["id"], "title": t["title"], "type": t["type"], "source": t["source"],
             "description": t["description"], "theme": t.get("theme"),
             "params": [{"name": p.get("name"), "required": bool(p.get("required")),
                         "default": p.get("default")} for p in t.get("params", [])]}
            for t in T.list_templates(type=type or None)]


@app.tool()
def new_survey(template_id: str, slug: str, title: str = "", params: dict = None,
               simulate: int = 0) -> dict:
    """Scaffold a new survey from a template into SURVEY_DIR/<slug>.survey.json (+ the readable .md) —
    the chat/cowork equivalent of `survey-agent new`. Substitutes the template's ${params}, validates,
    and REFUSES to write an invalid result. Pick `template_id` from `list_templates`; `slug` sets the
    survey id; `title` defaults from the slug; `params` supplies any other declared template params
    (e.g. {"service": "ChatGPT"}). Set `simulate` > 0 to run the coverage gate after writing. Returns
    {ok, saved, path, md_path, slug, template, source, errors, coverage?}."""
    import re
    from survey_agent import templates as T
    t = T.get(template_id)
    if not t:
        avail = ", ".join(sorted(x["id"] for x in T.list_templates())) or "(none)"
        return {"ok": False, "saved": False, "errors": [f"unknown template {template_id!r}; available: {avail}"]}
    if not re.match(r"^[a-zA-Z0-9_-]+$", slug or ""):
        return {"ok": False, "saved": False, "errors": [f"invalid slug {slug!r} — letters, digits, '-' or '_' only"]}
    vals = {"slug": slug, "title": title or slug.replace("-", " ").replace("_", " ").strip().title()}
    vals.update(params or {})
    missing = T.missing_params(t, vals)
    if missing:
        return {"ok": False, "saved": False, "errors": [f"missing required param(s): {', '.join(missing)}"],
                "params_needed": [p for p in t.get("params", []) if p.get("name") in missing]}
    try:
        spec = T.render(t, vals)
    except ValueError as e:
        return {"ok": False, "saved": False, "errors": [str(e)]}
    errors = validate_mod.validate(spec)
    if [e for e in errors if not e.startswith("[warn]")]:
        return {"ok": False, "saved": False, "errors": errors,
                "message": "the scaffold did not validate — adjust params and retry"}
    os.makedirs(SURVEYS_DIR, exist_ok=True)
    path = os.path.join(SURVEYS_DIR, f"{slug}.survey.json")
    with open(path, "w") as f:
        json.dump(spec, f, indent=2)
        f.write("\n")
    md_path = os.path.join(SURVEYS_DIR, f"{slug}.md")
    try:
        from survey_agent.engine.compile_md import render_markdown
        with open(md_path, "w") as f:
            f.write(render_markdown(spec))
    except Exception:
        md_path = None
    try:
        from survey_agent import registry
        registry.record_validation(slug, ok=True, sdir=SURVEYS_DIR)
    except Exception:
        pass
    result = {"ok": True, "saved": True, "path": path, "md_path": md_path, "slug": slug,
              "template": t["id"], "source": t["source"], "errors": errors}
    if simulate:
        from survey_agent import simulate as sim
        cov = sim.coverage(spec, n=simulate)
        try:
            from survey_agent import registry
            registry.record_test(slug, cov, sdir=SURVEYS_DIR)
        except Exception:
            pass
        result["coverage"] = sim.format_coverage(cov)
    return result


@app.tool()
def copy_survey(source: str, slug: str, title: str = "") -> dict:
    """Duplicate an existing survey into a new one — same content, fresh id/title/version and a clean
    lifecycle (it does NOT inherit the source's validated/tested/live state). The chat/cowork equivalent
    of `survey-agent copy`, and the third way to START a survey (alongside save_survey from a draft and
    new_survey from a template): "I need this exact survey again, maybe with a tweak." `source` is a
    survey_id from survey://catalog or a path; `slug` is the new survey id; `title` defaults to the
    source's. Writes SURVEY_DIR/<slug>.survey.json (+ the readable .md), validates, REFUSES to write an
    invalid result, and registers a fresh draft. Returns {ok, saved, path, md_path, slug, errors}."""
    import re
    if not re.match(r"^[a-zA-Z0-9_-]+$", slug or ""):
        return {"ok": False, "saved": False, "errors": [f"invalid slug {slug!r} — letters, digits, '-' or '_' only"]}
    try:
        spec = _load_survey_spec(source)
    except (ValueError, OSError) as e:
        return {"ok": False, "saved": False, "errors": [str(e)]}
    s = spec.get("survey", spec)
    s["id"] = slug
    if title:
        s["title"] = title
    s["version"] = 1                                # a copy starts its own version lineage at 1
    path = os.path.join(SURVEYS_DIR, f"{slug}.survey.json")
    if os.path.exists(path):
        return {"ok": False, "saved": False, "errors": [f"{path} already exists — choose another slug"]}
    errors = validate_mod.validate(spec)
    if [e for e in errors if not e.startswith("[warn]")]:
        return {"ok": False, "saved": False, "errors": errors,
                "message": "the copy did not validate — fix the source survey first"}
    os.makedirs(SURVEYS_DIR, exist_ok=True)
    with open(path, "w") as f:
        json.dump(spec, f, indent=2)
        f.write("\n")
    md_path = os.path.join(SURVEYS_DIR, f"{slug}.md")
    try:
        from survey_agent.engine.compile_md import render_markdown
        with open(md_path, "w") as f:
            f.write(render_markdown(spec))
    except Exception:
        md_path = None
    try:
        from survey_agent import registry
        registry.record_validation(slug, ok=True, sdir=SURVEYS_DIR)
    except Exception:
        pass
    return {"ok": True, "saved": True, "path": path, "md_path": md_path, "slug": slug, "errors": errors}


@app.tool()
def templatize_survey(source: str, template_id: str = "", title: str = "", type: str = "",
                      dest: str = "templates") -> dict:
    """Promote an existing survey into a reusable TEMPLATE (the chat/cowork equivalent of
    `survey-agent templatize`, the inverse of new_survey): tokenize id/title to ${slug}/${title}, lift
    theme + url_variables into a generated template.yaml, and write the pair under <dest>/<id>/ so it
    shows up in list_templates and can be stamped out with new_survey. Round-trip-verifies the result
    scaffolds back to a valid survey. `source` is a survey_id/path; `template_id` defaults to the source
    slug; `dest` is the templates dir ('templates' in-repo, or 'overlay/templates' for private). Add more
    ${params} to the manifest by hand for other knobs. Returns {ok, template_id, dir, manifest_path, errors}."""
    import re
    import yaml
    from survey_agent import templates as T
    try:
        spec = _load_survey_spec(source)
    except (ValueError, OSError) as e:
        return {"ok": False, "errors": [str(e)]}
    tid = template_id or (spec.get("survey", spec) or {}).get("id") or "template"
    if not re.match(r"^[a-zA-Z0-9_-]+$", tid):
        return {"ok": False, "errors": [f"invalid template_id {tid!r} — letters, digits, '-' or '_' only"]}
    manifest, spec = T.from_survey(spec, id=tid, title=title or None, type=type or None)
    tdir = os.path.join(dest, tid)
    if os.path.isdir(tdir):
        return {"ok": False, "errors": [f"{tdir} already exists — choose another template_id"]}
    os.makedirs(tdir, exist_ok=True)
    man_path = os.path.join(tdir, "template.yaml")
    spec_path = os.path.join(tdir, "survey.json")
    with open(man_path, "w") as f:
        yaml.safe_dump(manifest, f, sort_keys=False, default_flow_style=False)
    with open(spec_path, "w") as f:
        json.dump(spec, f, indent=2)
        f.write("\n")
    try:
        rendered = T.verify_dir(tid, manifest["params"], manifest.get("requires", {}), spec_path)
        errors = [e for e in validate_mod.validate(rendered) if not e.startswith("[warn]")]
    except ValueError as e:
        errors = [str(e)]
    return {"ok": not errors, "template_id": tid, "dir": tdir, "manifest_path": man_path, "errors": errors}


@app.tool()
def simulate_survey(survey_path: str = "", survey_json: dict = None, n: int = 500, quota: int = 0) -> str:
    """Run N synthetic respondents through a survey — the pre-launch coverage gate (asserts complete
    + every declared screen-out reachable and all callbacks mapped). Pass EITHER `survey_path` (a
    saved .survey.json / a survey_id from survey://catalog) OR `survey_json` (an inline draft you
    haven't saved yet — so the gate works on any surface, no file needed). Use after validate_survey.
    Returns the coverage report + the ✅/❌ gate verdict; records the result in the registry for a
    saved survey (visible in list_surveys)."""
    from survey_agent import simulate as sim
    if survey_json is not None:
        spec = survey_json
    elif survey_path:
        spec = _load_survey_spec(survey_path)
    else:
        return "error: pass either survey_path (a saved survey / id) or survey_json (an inline draft)"
    cov = sim.coverage(spec, n=n, quota_override=(quota or None))
    if survey_json is None:                     # only saved surveys have a registry home to record into
        try:
            from survey_agent import registry
            registry.record_test((spec.get("survey", spec) or {}).get("id"), cov, sdir=SURVEYS_DIR)
        except Exception:
            pass
    return sim.format_coverage(cov)


@app.tool()
def publish_survey(survey: str = "", survey_id: str = "", survey_json: dict = None) -> dict:
    """Publish an immutable version of a survey definition to the configured definition store
    (SURVEY_DEFINITION_SOURCE — file / mongo / http-gateway). Validates first; on `mongo`/`http` a content
    change auto-bumps the version and identical content is a no-op. This does NOT mark it live — going
    live is gated (use launch_survey, or set_survey_status with the approval token). Pass `survey` (a
    survey_id / path) or `survey_json`. Returns {ok, version, errors}."""
    from survey_agent import definitions
    spec = survey_json if survey_json is not None else _load_survey_spec(survey or survey_id)
    errors = validate_mod.validate(spec)
    if [e for e in errors if not e.startswith("[warn]")]:
        return {"ok": False, "errors": errors, "message": "not published — fix the validation errors first"}
    version = definitions.get_definition_store().put_definition(spec)
    return {"ok": True, "version": version, "errors": errors}


@app.tool()
def launch_survey(survey: str = "", survey_id: str = "", approval_token: str = "",
                  base_url: str = "", acknowledge_responsibilities: bool = False) -> dict:
    """Produce the drop-in **survey_url** an operator pastes into their own system (panel / CRM / email
    link), and — when approved — mark the survey **live**. The engine never contacts anyone; this just
    builds the URL from the survey's `url_variables` + the public base, and records state. `survey` is a
    survey_id (from survey://catalog) or a path to a .survey.json. Returns {survey_url, live, responsibilities, message}.

    You ALWAYS get the survey_url. MARKING IT LIVE needs TWO things from a human, so an agent can't field
    on its own: (1) the SURVEY_LAUNCH_APPROVAL_TOKEN passed via approval_token, and (2) the operator
    confirming the pre-fielding responsibilities (store/erasure/consent/URL integrity) by setting
    acknowledge_responsibilities=true. Show `responsibilities` to the human and have them confirm first."""
    from survey_agent import preflight
    spec = _load_survey_spec(survey or survey_id)
    s = spec.get("survey", spec)
    sid = s.get("id")
    from survey_agent import hosting
    base = hosting.public_base_url(base_url)
    uvars = (s.get("settings", {}) or {}).get("url_variables") or ["uid", "rid", "group"]
    url = f"{base}/s/{sid}?" + "&".join(f"{u}={{{u}}}" for u in uvars)
    approved = _launch_approved(approval_token)
    live = approved and bool(acknowledge_responsibilities)
    if live:
        try:
            from survey_agent import registry
            registry.set_status(sid, status="live", url=url, sdir=SURVEYS_DIR)
        except Exception:
            pass
    if live:
        message = "Marked live. Drop this URL into your panel/CRM/email; replace each {placeholder} per respondent."
    elif approved and not acknowledge_responsibilities:
        message = ("Approved, but NOT live — have the human confirm the `responsibilities` checklist, then call "
                   "again with acknowledge_responsibilities=true. The survey_url below is already usable.")
    else:
        message = ("Usable survey_url below — replace each {placeholder} per respondent. To also mark it LIVE, a "
                   "human sets SURVEY_LAUNCH_APPROVAL_TOKEN, passes approval_token, and confirms the checklist "
                   "(acknowledge_responsibilities=true).")
    out = {
        "survey_url": url, "live": live,
        "base_url_source": hosting.base_url_source(base_url),
        "responsibilities": preflight.items(),
        "message": message,
        "serve_with": "survey-agent serve, or your hosted runtime",
    }
    # Tell the agent when the URL it just produced is unreachable, so it reports that instead of
    # handing a localhost link to a human as if it were fieldable.
    unreachable = hosting.unreachable_warning(base)
    if unreachable:
        out["unreachable"] = unreachable
    return out


@app.tool()
def survey_report(survey: str = "", survey_id: str = "", survey_json: dict = None, simulate: int = 0) -> dict:
    """Fielding stats + question analytics — funnel (completed / screened-out / over-quota), incidence,
    LOI, per-question distributions, and quota burndown. Reads the configured response store, or set
    `simulate=N` for synthetic respondents when there's little/no real data. Pass `survey` (a survey_id /
    path) or `survey_json`. Returns the structured report dict."""
    from survey_agent import report as report_mod
    from survey_agent.engine.runtime import SurveyRuntime
    spec = survey_json if survey_json is not None else _load_survey_spec(survey or survey_id)
    rt = SurveyRuntime(spec)
    if simulate:
        rows, quotas = report_mod.collect_via_simulation(rt, simulate, 7)
    else:
        import survey_agent.store as store_pkg
        rows, quotas = report_mod.collect_from_store(rt, store_pkg.get_store())
    return report_mod.build_report(rt, rows, quotas)


@app.tool()
def list_surveys() -> list:
    """The survey-management view: every survey in this deployment with its purpose (`description`),
    `tags`, `version`, and lifecycle `state` (draft → validated → tested → live), plus the last test's
    incidence/LOI and any live URL. Call this to see what exists, what's been tested, and what's live
    before authoring, editing, or launching — the index for managing a series of surveys."""
    from survey_agent import registry
    return registry.catalog(SURVEYS_DIR)


@app.tool()
def build_dashboard(out: str = "dashboard.html", simulate: int = 0) -> dict:
    """Write a self-contained HTML management dashboard (every survey + purpose + lifecycle state,
    deep-linking into each survey's report) to `out`; `simulate=N` also builds each report from N
    synthetic respondents. Returns {path, surveys, reports}. The way to hand a human a visual
    overview of the whole survey program from chat — they open the file in a browser."""
    from survey_agent import registry, dashboard
    cat = registry.catalog(SURVEYS_DIR)
    report_links = {}
    if simulate:
        from survey_agent import report as report_mod
        from survey_agent.engine.runtime import SurveyRuntime
        outdir = os.path.dirname(os.path.abspath(out))
        for e in cat:
            if not e.get("file"):
                continue
            try:
                rt = SurveyRuntime(_load_survey_spec(e["id"]))
                html = report_mod.generate(rt, simulate_n=simulate, seed=7)
                rp = f"{e['id']}-report.html"
                with open(os.path.join(outdir, rp), "w") as f:
                    f.write(html)
                report_links[e["id"]] = rp
            except Exception:
                pass
    path = os.path.abspath(out)
    with open(path, "w") as f:
        f.write(dashboard.render_html(cat, report_links=report_links))
    return {"path": path, "surveys": len(cat), "reports": len(report_links)}


@app.tool()
def pretest_survey(survey: str = "", survey_json: dict = None, n: int = 0, audience: dict = None) -> dict:
    """Persona pre-test: run synthetic *role-played* respondents (not coin flips) to preview realistic
    incidence + who screens out and why — richer than the uniform-random coverage gate. Pass EITHER
    `survey` (a survey_id / path) OR `survey_json` (an inline draft). `n` sets the count (default: a
    small sample, or the audience size); `audience` is an optional weighted-segment population to test
    against. Offline-safe (uses the deterministic stub with no model key). Returns {incidence, n,
    statuses, reasons (screen-out tallies), source}. Run after simulate_survey to sanity-check targeting."""
    from survey_agent import simulate as sim
    spec = survey_json if survey_json is not None else _load_survey_spec(survey)
    r = sim.persona_pretest(spec, n=(n or None), audience=audience)
    return {"id": r.get("id"), "incidence": round(r.get("incidence", 0), 3), "n": r.get("n"),
            "statuses": r.get("statuses"), "reasons": r.get("reasons"), "source": r.get("source"),
            "report": sim.format_pretest(r)}   # human-readable: incidence + outcomes + answer distributions


@app.tool()
def survey_insights(survey: str = "", survey_json: dict = None, simulate: int = 0) -> dict:
    """AI analyst read-out of results — headline, key findings, open-end themes, watch-outs, suggested
    follow-ups. Pass EITHER `survey` (id / path) OR `survey_json` (inline). Reads real responses from the
    configured store, or set `simulate=N` to analyse N synthetic respondents when there's little/no real
    data yet. Offline-safe: with no model key it returns a deterministic heuristic read-out — the
    `engine` field says which. Opt-in: this is the one analysis tool that may send response data to an
    LLM. Returns {engine, headline, findings, themes, watchouts, next_steps, n}."""
    from survey_agent import report as report_mod, insights as insights_mod
    from survey_agent.engine.runtime import SurveyRuntime
    spec = survey_json if survey_json is not None else _load_survey_spec(survey)
    rt = SurveyRuntime(spec)
    if simulate:
        rows, quotas = report_mod.collect_via_simulation(rt, simulate, 7)
    else:
        import survey_agent.store as store_pkg
        rows, quotas = report_mod.collect_from_store(rt, store_pkg.get_store())
    rep = report_mod.build_report(rt, rows, quotas)
    ins = insights_mod.generate_insights(rt, rows, rep)
    return {"engine": ins.get("engine"), "headline": ins.get("headline"),
            "findings": ins.get("findings", []), "themes": ins.get("themes", []),
            "watchouts": ins.get("watchouts", []), "next_steps": ins.get("next_steps", []),
            "n": rep.get("n")}


@app.tool()
def export_survey(survey: str = "", survey_json: dict = None, simulate: int = 0, format: str = "csv") -> dict:
    """Export the tabular response grid (one row per respondent; columns for url params, each question,
    and derived fields) — the stable shape DSAR / analysis tooling expects. Pass EITHER `survey` (id /
    path) OR `survey_json` (inline). Reads the configured store, or `simulate=N` for synthetic rows.
    `format` is 'csv' (returns the CSV text in `csv`) or 'json' (returns {columns, rows}). Free-text is
    included; file-upload answers are references, not bytes. Returns {format, n, columns, csv|rows}."""
    from survey_agent import exporting
    from survey_agent.engine.runtime import SurveyRuntime
    spec = survey_json if survey_json is not None else _load_survey_spec(survey)
    rt = SurveyRuntime(spec)
    store = None
    if not simulate:
        import survey_agent.store as store_pkg
        store = store_pkg.get_store()
    responses = exporting.collect_responses(rt, store=store, simulate_n=simulate, seed=7)
    cols, rows = exporting.flatten(rt, responses)
    if format == "json":
        return {"format": "json", "n": len(rows), "columns": cols, "rows": rows}
    return {"format": "csv", "n": len(rows), "columns": cols, "csv": exporting.to_csv(cols, rows)}


@app.tool()
def set_survey_status(survey: str = "", survey_id: str = "", status: str = "", note: str = None,
                      project: str = None, url: str = "", approval_token: str = "",
                      acknowledge_responsibilities: bool = False) -> dict:
    """Set a survey's lifecycle state / note / project — mutable registry metadata; it never edits the
    immutable definition (so moving/relabelling doesn't bump the version). `status` ∈
    draft|validated|tested|approved|paused|closed (set freely). `project` moves it to an organizational
    group ("" ungroups). `note` attaches free text. GOING LIVE IS GATED LIKE launch_survey: to set
    status='live' a human must pass approval_token (SURVEY_LAUNCH_APPROVAL_TOKEN) AND
    acknowledge_responsibilities=true — an agent cannot open a survey to real respondents on its own.
    Returns {ok, id, state, project, live, message}."""
    from survey_agent import registry
    sid = survey or survey_id
    if not sid:
        return {"ok": False, "error": "pass `survey` (a survey_id) or `survey_id`"}
    if status and status not in registry.STATES:
        return {"ok": False, "error": f"unknown status {status!r}; choose from {', '.join(registry.STATES)}"}
    if status == "live" and not (_launch_approved(approval_token) and acknowledge_responsibilities):
        from survey_agent import preflight
        return {"ok": False, "live": False, "responsibilities": preflight.items(),
                "message": "Marking LIVE is gated: a human passes approval_token "
                           "(SURVEY_LAUNCH_APPROVAL_TOKEN) and sets acknowledge_responsibilities=true. "
                           "Other states (draft/validated/tested/approved/paused/closed), note and "
                           "project are set freely."}
    registry.set_status(sid, status=(status or None), note=note, url=(url or None),
                        project=project, sdir=SURVEYS_DIR)
    entry = next((e for e in registry.catalog(SURVEYS_DIR) if e["id"] == sid), {})
    state = entry.get("state")
    return {"ok": True, "id": sid, "state": state, "project": entry.get("project"),
            "live": state == "live",
            "message": f"{sid} → {state}" + (f" · project {entry['project']}" if entry.get("project") else "")}


# --- survey-as-a-tool: run a logic-gated survey inline to collect clean structured data from a human ---
@app.tool()
def begin_survey(survey: str = "", survey_json: dict = None, url_variables: dict = None) -> dict:
    """Run a survey as a guided conversation so YOU (an agent) can collect clean, logic-gated,
    structured answers from a person mid-task — the structured-elicitation primitive. Pass EITHER
    `survey` (a survey_id from survey://catalog or a path to a .survey.json) OR `survey_json` (an
    inline spec). Returns a `session_token` and the first `message` to show the person; relay it,
    then call answer_survey() with their reply until `done`. The engine enforces ALL
    branching/screen-out/quotas/validation — you only pass messages through; never decide eligibility."""
    from survey_agent.conversation import Conversation
    import uuid
    spec = survey_json if survey_json is not None else _load_survey_spec(survey)
    convo = Conversation(spec, session_id="mcp:" + uuid.uuid4().hex[:8],
                         url_vars=url_variables or {})
    opening = convo.opening()
    token = "sv_" + uuid.uuid4().hex[:12]
    _CONVOS[token] = convo
    return {"session_token": token, "message": opening["message"],
            "question_id": opening.get("question_id"), "done": opening.get("done", False)}


@app.tool()
def answer_survey(session_token: str, reply: str) -> dict:
    """Continue a survey started with begin_survey: submit the person's free-text `reply`. The engine
    maps it to a valid answer and advances (re-asking if it was invalid). While going, returns the
    next `message` + `done:false`. When `done:true`, returns the structured `result`
    ({status, reason_code, survey_data{qid:{question,answer}}, derived{...}}) — the clean,
    logic-validated data you came for — and the session is closed."""
    convo = _CONVOS.get(session_token)
    if not convo:
        return {"error": "unknown or expired session_token — call begin_survey first", "done": True}
    out = convo.reply(reply)
    resp = {"message": out["message"], "done": bool(out.get("done"))}
    if out.get("done"):
        from survey_agent.engine import export as export_mod
        resp["status"] = out.get("status")
        resp["result"] = export_mod.response_from_session(convo.rt, convo.session)
        _CONVOS.pop(session_token, None)
    else:
        resp["question_id"] = out.get("question_id")
    return resp


# ----------------------------------------------------------------- resources
@app.resource("survey://schema")
def res_schema() -> str:
    """The canonical Survey JSON Schema — author against this."""
    return open(SCHEMA_PATH).read()


@app.resource("survey://catalog")
def res_catalog() -> str:
    """Surveys in this deployment with purpose + lifecycle state (the management view — same data as
    the `list_surveys` tool): id, title, description, tags, version, state, last test, live URL."""
    from survey_agent import registry
    return json.dumps(registry.catalog(SURVEYS_DIR), indent=2)


@app.resource("survey://themes")
def res_themes() -> str:
    """Available look-and-feel themes and their design tokens."""
    import survey_agent.themes as themes
    return json.dumps({t: themes.tokens(t) for t in themes.list_themes()}, indent=2)


@app.resource("survey://templates")
def res_templates() -> str:
    """Survey scaffolds available to `new_survey` (core + plugins + overlay), with their params —
    the same catalog the `list_templates` tool returns."""
    from survey_agent import templates as T
    keys = ("id", "title", "type", "source", "description", "theme", "params")
    return json.dumps([{k: t.get(k) for k in keys} for t in T.list_templates()], indent=2)


@app.resource("survey://{survey_id}")
def res_survey(survey_id: str) -> str:
    """The full definition of one survey by id."""
    path = os.path.join(SURVEYS_DIR, f"{survey_id}.survey.json")
    return open(path).read() if os.path.exists(path) else json.dumps({"error": f"unknown survey {survey_id}"})


# ----------------------------------------------------------------- prompts
@app.prompt()
def author_survey(brief: str = "") -> str:
    """The survey-agent authoring workflow, seeded with a natural-language brief."""
    return (
        "You are authoring a survey for the survey-agent engine. Follow this loop and do NOT launch "
        "without explicit human approval:\n\n"
        "1. Clarify only what's missing: audience, screen-out criteria, quota cells, ~length, where "
        "qualifiers route, any behavioral/contextual enrichment, theme.\n"
        "2. Draft the Survey JSON against the `survey://schema` resource. Give it a `description` "
        "(what it's for) and optional `tags`/`owner` so it shows up usefully in `list_surveys`. A "
        "question that depends on a PRIOR answer goes on its own LATER page. Every screen-out is a "
        "`disqualify` with a `reason_code`; include one terminal `complete`; declare every url_var "
        "and context source you reference. To ask the same thing for each option chosen earlier, "
        "use `repeat` on a page or question (answers land as `qid[item]`; pipe `{{loop:label}}`).\n"
        "3. Call `validate_survey` and fix every error.\n"
        "4. Call `save_survey` to persist it to SURVEY_DIR (so it's a managed file the gate + runtime "
        "can use). On a surface without file tools you can also pass the spec inline to the next step.\n"
        "5. Call `simulate_survey` (n=500) — the coverage gate must pass.\n"
        "6. Summarize the flow + terminals, then STOP for human review.\n"
        "7. Only after approval: `publish_survey` then `launch_survey` (needs the server approval token).\n\n"
        f"Brief: {brief or '(none provided — ask the user for one)'}"
    )


@app.prompt()
def review_before_launch(survey_id: str = "") -> str:
    """A reviewer checklist before launching a survey."""
    return (
        f"Review survey '{survey_id}' before launch. Confirm: (a) `validate_survey` is clean; "
        "(b) `simulate_survey` coverage gate passed and incidence looks plausible; (c) every "
        "screen-out has a reason; (d) qualifiers route to the intended task; (e) any context/"
        "enrichment pages are gated so they no-op when the fact is absent; (f) the theme is right. "
        "If all pass, a human approves and supplies the launch approval token."
    )


def main(argv=None):
    """Console entry point (`survey-agent-mcp`). stdio by default for local clients (Claude Code,
    Cursor, Claude Desktop); --transport sse|streamable-http for a remote connector (put OAuth in front)."""
    ap = argparse.ArgumentParser(prog="survey-agent-mcp", description="survey-agent MCP server")
    ap.add_argument("--transport", default="stdio", choices=["stdio", "sse", "streamable-http"],
                    help="stdio (local) or sse/streamable-http (remote connector; put OAuth in front)")
    args, _ = ap.parse_known_args(argv)
    # same deployment defaults as the CLI: env (incl. .env) > survey-agent.config.yaml > built-in
    # (SURVEY_STORE=file, SURVEY_INTEGRATION=none) — so report/responses tools see what serve collected
    from survey_agent import config
    config.apply_defaults()
    try:
        app.run(transport=args.transport)
    except TypeError:  # older FastMCP without a transport kwarg
        app.run()


if __name__ == "__main__":
    main()
