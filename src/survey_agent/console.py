"""Operator console — a local, read-first GUI onto the same engine actions the agent runs.

Binds **127.0.0.1** by default (an operator surface, never respondents). It can be hosted for a team
(`--host 0.0.0.0` / `SURVEY_CONSOLE_HOST`), but it is **unauthenticated** and exposes operator controls,
so put it behind an auth proxy / VPN / trusted network — never the open internet. Visibility first: a board of every
survey with a status light, completes, and incidence; click a row for a read-only preview (pages →
questions → logic → terminals) and one-click ops — pause / resume / close, generate a report, pull
responses, copy the drop-in URL. New surveys come from the template catalog (browsable); authoring
itself stays with the agent — this is just a different interface to the same engine.

Pure stdlib. Reuses `definitions` (list/get/status), `report.generate`, the response `store`, and the
`templates` catalog — it reimplements nothing.
"""
import json
import os
import urllib.parse
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ----------------------------------------------------------------- engine glue
# Cache the store/definition-store instances. Building them is cheap for memory/file but EXPENSIVE for
# mongo (a new MongoClient + index round-trips per call) — and the board builds one per survey, so an
# uncached `get_store()` made a board load N connections deep. Keyed by the env that determines identity
# so tests (which monkeypatch SURVEY_* / dirs per case) still get a fresh instance when it changes.
_DEFSTORE_CACHE = {"key": None, "val": None}
_RESPSTORE_CACHE = {"key": None, "val": None}


def _defstore():
    from survey_agent import definitions
    key = (os.environ.get("SURVEY_DEFINITION_SOURCE", "file"), os.environ.get("SURVEY_DIR", "surveys"),
           os.environ.get("SURVEY_MONGODB_URI", ""), os.environ.get("SURVEY_MONGODB_DB", ""))
    if _DEFSTORE_CACHE["key"] != key:
        _DEFSTORE_CACHE.update(key=key, val=definitions.get_definition_store())
    return _DEFSTORE_CACHE["val"]


def _respstore():
    from survey_agent import store
    key = ((os.environ.get("SURVEY_STORE") or "memory"), os.environ.get("SURVEY_DATA_DIR", ""),
           os.environ.get("SURVEY_MONGODB_URI", ""), os.environ.get("SURVEY_MONGODB_DB", ""))
    if _RESPSTORE_CACHE["key"] != key:
        _RESPSTORE_CACHE.update(key=key, val=store.get_store())
    return _RESPSTORE_CACHE["val"]


def _public_base():
    from survey_agent import hosting
    return hosting.public_base_url()


def _console_brand():
    """Console display name. Defaults to the open-core 'survey-agent'; a deployment overrides it via
    SURVEY_CONSOLE_BRAND. Kept as an env override so upstream syncs don't conflict on a hardcoded name."""
    return os.environ.get("SURVEY_CONSOLE_BRAND", "survey-agent")


def _host_label():
    """Rail-footer label for the operator surface: the configured public base host, marking a local
    bind as such. Replaces a hardcoded '127.0.0.1' that was wrong on any non-local deployment."""
    host, _port, is_local = _runtime_target()
    return f"local · {host}" if is_local else host


def _render_index():
    """Serve INDEX_HTML with the brand + host footer injected (the template ships open-core defaults)."""
    brand = escape(_console_brand())
    return (INDEX_HTML
            .replace("<title>survey-agent · console</title>", f"<title>{brand} · console</title>")
            .replace("survey-agent<small>operator console</small>", f"{brand}<small>operator console</small>")
            .replace("<div>local · 127.0.0.1</div>", f"<div>{escape(_host_label())}</div>"))


def _preview_token():
    """The token the runtime requires to preview a not-yet-live survey (SURVEY_PREVIEW_TOKEN). When the
    console auto-starts its local runtime it mints one (see _maybe_start_runtime), so operator preview
    works out of the box; against an external runtime, set the same token on both."""
    return os.environ.get("SURVEY_PREVIEW_TOKEN")


def _preview_url(survey_id, spec=None):
    """The operator 'open as respondent' URL — a preview (collects nothing), carrying the preview token
    so it bypasses the live gate. Distinct from the real drop-in `url`, which respondents use once live.

    Carries test values for the survey's *configured* identity variables (settings.identity,
    e.g. custom respondent/response ids) rather than the engine defaults — so a walk-through exercises the same identity a real
    invite would, including the terminal redirect's id passback. Falls back to the defaults (uid/rid)
    when no spec is supplied."""
    from survey_agent.engine.runtime import SurveyRuntime, DEFAULT_RESPONDENT_VAR, DEFAULT_RESPONSE_VAR
    respondent_var, response_var = DEFAULT_RESPONDENT_VAR, DEFAULT_RESPONSE_VAR
    if spec:
        try:
            rt = SurveyRuntime(spec)
            respondent_var, response_var = rt.respondent_var, rt.response_var
        except Exception:
            pass
    params = {respondent_var: "preview", response_var: "preview-1", "group": "preview"}
    tok = _preview_token()
    if tok:
        params["preview"] = tok                           # last, so the gate token reads cleanly off the tail
    return f'{_public_base().rstrip("/")}/s/{survey_id}?' + urllib.parse.urlencode(params)


def _runtime_target():
    """Parse the respondent base URL into (host, port, is_local) — the runtime that actually serves
    `/s/<id>` to respondents (the console itself never does; it's operator-only)."""
    u = urllib.parse.urlparse(_public_base())
    host = u.hostname or "localhost"
    port = u.port or (443 if u.scheme == "https" else 80)
    return host, port, host in ("localhost", "127.0.0.1", "0.0.0.0", "::1")


def _port_open(host, port, timeout=0.25):
    import socket
    try:
        with socket.create_connection(("127.0.0.1" if host in ("0.0.0.0", "::1") else host, port), timeout):
            return True
    except OSError:
        return False


def runtime_status():
    """Is a respondent runtime reachable at the configured base URL? The console links to it for
    'open as respondent' / live preview, but it's a separate server — surface whether it's up so a
    'live' survey that nobody is serving doesn't look broken."""
    host, port, local = _runtime_target()
    return {"base": _public_base().rstrip("/"), "host": host, "port": port,
            "local": local, "up": _port_open(host, port)}


def _counts_from(bucket):
    """Normalise a {complete, disqualified, total} tally → the board's completes/screenouts/incidence."""
    b = bucket or {}
    comp, dq = b.get("complete", 0), b.get("disqualified", 0)
    return {"completes": comp, "screenouts": dq, "responses": b.get("total", comp + dq),
            "incidence": round(comp / (comp + dq), 3) if (comp + dq) else None}


def _counts(survey_id):
    """completes / screen-outs / incidence for ONE survey (the detail drawer). Uses the store's fast
    `count_responses` when available, else counts `list_responses`."""
    store = _respstore()
    cr = getattr(store, "count_responses", None)
    if cr:
        try:
            return _counts_from(cr(survey_id))
        except Exception:
            pass
    try:
        rows = store.list_responses(survey_id)
    except Exception:
        rows = []
    comp = sum(1 for r in rows if r.get("status") == "Complete")
    dq = sum(1 for r in rows if r.get("status") == "Disqualified")
    return _counts_from({"complete": comp, "disqualified": dq, "total": len(rows)})


def overview():
    ds = _defstore()
    rows, vit = [], {"surveys": 0, "live": 0, "paused": 0, "closed": 0, "other": 0, "completes": 0}
    # one grouped tally for the whole board (a single aggregation/pass) instead of a count per survey
    by_survey = None
    grouped = getattr(_respstore(), "count_responses_by_survey", None)
    if grouped:
        try:
            by_survey = grouped()
        except Exception:
            by_survey = None
    for e in ds.list_surveys():
        if e.get("missing_file"):
            continue
        c = _counts_from(by_survey.get(e["id"])) if by_survey is not None else _counts(e["id"])
        state = e.get("state") or "draft"
        row = {"id": e["id"], "title": e.get("title") or e["id"], "state": state,
               "version": e.get("version"), "tags": e.get("tags", []), "owner": e.get("owner"),
               "project": e.get("project"),
               "updated": (e.get("updated_at") or (e.get("live") or {}).get("at")),
               "url": f'{_public_base().rstrip("/")}/s/{e["id"]}', **c}
        rows.append(row)
        vit["surveys"] += 1
        vit["completes"] += c["completes"]
        vit["live" if state == "live" else "paused" if state == "paused"
            else "closed" if state == "closed" else "other"] += 1
    rows.sort(key=lambda r: ({"live": 0, "paused": 1, "tested": 2, "validated": 3, "draft": 4,
                              "closed": 5}.get(r["state"], 3), r["title"].lower()))
    return {"vitals": vit, "surveys": rows}


def _q_view(q):
    return {"id": q.get("id"), "type": q.get("type"), "title": q.get("title") or "",
            "required": bool(q.get("required")), "body": q.get("body"),
            "media": q.get("media"), "has_logic": bool(q.get("display_logic")),
            "repeat": (q.get("repeat") or {}).get("source"),
            "validation": q.get("validation"),
            "options": [{"value": o.get("value"), "label": o.get("label", o.get("value")),
                         "exclusive": bool(o.get("exclusive"))} for o in (q.get("options") or [])],
            "rows": [r.get("label", r.get("id")) for r in (q.get("rows") or [])]}


def survey_detail(survey_id):
    spec = _defstore().get_definition(survey_id)
    if not spec:
        return None
    s = spec.get("survey", {})
    reg = _defstore().get_registry(survey_id) or {}
    pages = [{"id": p.get("id"), "title": p.get("title"), "repeat": (p.get("repeat") or {}).get("source"),
              "questions": [_q_view(q) for q in p.get("questions", [])]} for p in s.get("pages", [])]
    actions = [{"type": a.get("type"), "reason_code": a.get("reason_code"),
                "gated": bool(a.get("when")), "redirect": (a.get("redirect") or {}).get("kind")}
               for a in s.get("actions", []) if a.get("type") in ("disqualify", "overquota", "complete", "skip")]
    return {"id": survey_id, "title": s.get("title"), "description": s.get("description"),
            "version": reg.get("live_version") or s.get("version"), "state": reg.get("status") or "draft",
            "owner": s.get("owner"), "tags": s.get("tags", []), "project": reg.get("project"),
            "url_variables": s.get("settings", {}).get("url_variables", []),
            "launch_gated": bool(os.environ.get("SURVEY_LAUNCH_APPROVAL_TOKEN")),
            "url": f'{_public_base().rstrip("/")}/s/{survey_id}',
            "preview_url": _preview_url(survey_id, spec), "pages": pages, "actions": actions,
            **_counts(survey_id)}


class LaunchGated(PermissionError):
    """Marking live was refused by the launch gate (SURVEY_LAUNCH_APPROVAL_TOKEN)."""


def set_status(survey_id, status, approve=None):
    from survey_agent import registry, config
    if status not in registry.STATES:
        raise ValueError(f"unknown status {status!r}")
    if status == "live":
        ok, why = config.may_mark_live(approve, surface="console")
        if not ok:
            raise LaunchGated(f"not marked live: {why}.")
    fields = {"status": status}
    if status == "live":
        # No explicit live_version: an approved mark-live pins the newest saved version (the store does
        # it), the same as the CLI and the gateway. get_definition(id) would return the CURRENT pin,
        # which would silently keep serving the old version after an edit.
        fields["live"] = {"at": registry.now()}
    _defstore().set_registry(survey_id, **fields)
    return survey_detail(survey_id)


def set_project(survey_id, project):
    """Move a survey to a project (a mutable organizational group, stored in the registry — not the
    immutable definition, so moving never bumps the version). Empty/blank → ungrouped."""
    if not _defstore().get_definition(survey_id):
        raise ValueError(f"unknown survey {survey_id!r}")
    _defstore().set_registry(survey_id, project=(project or "").strip() or None)
    return survey_detail(survey_id)


def report_html(survey_id, simulate_n=0):
    from survey_agent.engine.runtime import SurveyRuntime
    from survey_agent import report
    spec = _defstore().get_definition(survey_id)
    if not spec:
        return None
    rt = SurveyRuntime(spec)
    store = None if simulate_n else _respstore()
    return report.generate(rt, store=store, simulate_n=simulate_n, seed=7)


def _insights_fragment(ins, n, simulated, live):
    """Render the analyst read-out as a console-styled HTML fragment (the report's own renderer uses
    the report page's CSS vars, which don't exist in the console)."""
    from html import escape
    src = "a simulation (no live responses yet)" if simulated else f"{n} response(s)"
    eng = escape(str(ins.get("engine", "?")))
    kind = "real model" if live else "offline heuristic stub — add an API key for narrative open-end analysis"
    li = lambda xs: "".join(f'<div class="cl">• {escape(str(x))}</div>' for x in xs)
    out = [f'<div class=checks><div class="chk ok">🧠 AI read-out · engine: {eng} ({kind}) · from {src}</div>']
    if ins.get("headline"):
        out.append(f'<div class=cl style="color:var(--ink);font-size:13px;padding:6px 11px"><b>{escape(str(ins["headline"]))}</b></div>')
    if ins.get("findings"):
        out.append('<div class=sec>Key findings</div>' + li(ins["findings"]))
    if ins.get("themes"):
        out.append('<div class=sec>Open-end themes</div>' + "".join(
            f'<div class="cl">• <b>{escape(str(t.get("theme","")))}</b> — {escape(str(t.get("note","")))}</div>'
            for t in ins["themes"]))
    if ins.get("watchouts"):
        out.append('<div class=sec>Watch-outs</div>' + li(ins["watchouts"]))
    if ins.get("next_steps"):
        out.append('<div class=sec>Suggested follow-ups</div>' + li(ins["next_steps"]))
    return "".join(out) + "</div>"


def insights_html(survey_id, simulate_n=0):
    """AI analyst read-out (headline, findings, open-end themes, watch-outs, next steps). Opt-in click —
    it's the one console action that may call an LLM (falls back to a deterministic offline stub with no
    key). On real responses it reads them; with few/none it can run on a simulation (clearly labelled)."""
    from survey_agent.engine.runtime import SurveyRuntime
    from survey_agent import report, insights as ins_mod, ai
    spec = _defstore().get_definition(survey_id)
    if not spec:
        return None
    rt = SurveyRuntime(spec)
    if simulate_n:
        rows, quotas = report.collect_via_simulation(rt, simulate_n, 7)
    else:
        rows, quotas = report.collect_from_store(rt, _respstore())
    rep = report.build_report(rt, rows, quotas)
    ins = ins_mod.generate_insights(rt, rows, rep)
    return _insights_fragment(ins, rep.get("n", len(rows)), simulated=bool(simulate_n), live=ai.has_real_llm())


def validate_survey(survey_id):
    """Run the schema + semantic lint (the `validate` check) and record the result on the lifecycle.
    Pure-engine, always available. A clean pass advances a draft to `validated`."""
    from survey_agent.engine import validate as v
    from survey_agent import registry
    spec = _defstore().get_definition(survey_id)
    if not spec:
        return None
    errs = v.validate(spec)
    warnings = [e for e in errs if e.startswith("[warn]")]
    errors = [e for e in errs if not e.startswith("[warn]")]
    ok = not errors
    fields = {"validated": {"ok": ok, "at": registry.now()}}
    if ok and (_defstore().get_registry(survey_id) or {}).get("status") in (None, "draft"):
        fields["status"] = "validated"
    _defstore().set_registry(survey_id, **fields)
    return {"id": survey_id, "ok": ok, "errors": errors, "warnings": warnings,
            "state": survey_detail(survey_id)["state"]}


def simulate_survey(survey_id, n=200):
    """Run the coverage gate (`simulate -n`) — N synthetic respondents through every path — and record
    it on the lifecycle. A clean pass advances a draft/validated survey to `tested`."""
    from survey_agent import simulate, registry
    spec = _defstore().get_definition(survey_id)
    if not spec:
        return None
    cov = simulate.coverage(spec, n=int(n or 200))
    ok = not cov["problems"]
    loi = cov.get("loi_seconds")
    loi_min = round(loi / 60, 1) if loi else None
    fields = {"tested": {"ok": ok, "at": registry.now(), "n": cov["n"], "loi_min": loi_min,
                         "gate": "passed" if ok else "failed"}}
    if ok and (_defstore().get_registry(survey_id) or {}).get("status") in (None, "draft", "validated"):
        fields["status"] = "tested"
    _defstore().set_registry(survey_id, **fields)
    return {"id": survey_id, "n": cov["n"], "ok": ok, "problems": cov["problems"],
            "statuses": cov["statuses"], "fired_reasons": cov["fired_reasons"],
            "defined_reasons": cov["defined_reasons"], "loi_min": loi_min,
            "state": survey_detail(survey_id)["state"]}


def template_catalog():
    from survey_agent import templates
    return templates.list_templates()


def template_detail(template_id):
    from survey_agent import templates
    t = templates.get(template_id)
    if not t:
        return None
    out = dict(t)
    spec_path = t.get("spec_path") or t.get("path")
    try:
        spec = json.load(open(spec_path))
        s = spec.get("survey", spec)
        out["preview"] = [{"id": p.get("id"), "title": p.get("title"),
                           "repeat": (p.get("repeat") or {}).get("source"),
                           "questions": [_q_view(q) for q in p.get("questions", [])]}
                          for p in s.get("pages", [])]
    except Exception:
        out["preview"] = []
    return out


def _fill_placeholders(obj, mapping):
    """Recursively substitute ${...} placeholders in every string of a template spec. Unmapped
    placeholders (e.g. ${service}) are left intact as author-fill TODOs."""
    if isinstance(obj, str):
        for k, v in mapping.items():
            obj = obj.replace(k, v)
        return obj
    if isinstance(obj, list):
        return [_fill_placeholders(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: _fill_placeholders(v, mapping) for k, v in obj.items()}
    return obj


def create_from_template(template_id, survey_id, title=None):
    """Scaffold a new DRAFT survey from a template (a mechanical copy + id). The agent then fills it in
    and validates — the console doesn't author, it stamps out a starting point."""
    import re
    from survey_agent import templates
    t = templates.get(template_id)
    if not t:
        raise ValueError(f"unknown template {template_id!r}")
    sid = re.sub(r"[^a-z0-9_-]+", "-", (survey_id or "").lower()).strip("-")
    if not sid:
        raise ValueError("a survey id is required")
    if _defstore().get_definition(sid):
        raise ValueError(f"survey '{sid}' already exists")
    raw = json.load(open(t.get("spec_path") or t.get("path")))
    surv = raw.get("survey", raw)
    # Templates carry ${slug}/${title} placeholders (in the title *and* nested fields); fill them
    # everywhere so none leak into the draft. Title defaults to a humanized id when none was supplied.
    title = title or sid.replace("-", " ").replace("_", " ").strip().title()
    surv = _fill_placeholders(surv, {"${slug}": sid, "${title}": title})
    surv["id"] = sid                                      # authoritative, regardless of template form
    surv["version"] = 1
    _defstore().put_definition({"survey": surv})
    _defstore().set_registry(sid, status="draft", note=f"new draft from template '{template_id}'")
    return {"ok": True, "id": sid}


def copy_survey(source_id, new_id, title=None):
    """Duplicate an existing survey into a new DRAFT — same content, but a fresh id, version 1, and a
    clean lifecycle (the copy does NOT inherit the source's validated/tested/live state). The store-backed
    sibling of `create_from_template` and the console's one-click 'Copy…': the operator renames here, then
    edits from there. Internal page/question/action ids are kept; only the survey id changes."""
    import copy
    import re
    src = _defstore().get_definition(source_id)
    if not src:
        raise ValueError(f"unknown survey {source_id!r}")
    sid = re.sub(r"[^a-z0-9_-]+", "-", (new_id or "").lower()).strip("-")
    if not sid:
        raise ValueError("a new survey id is required")
    if _defstore().get_definition(sid):
        raise ValueError(f"survey '{sid}' already exists")
    surv = copy.deepcopy(src.get("survey", src))         # fully decouple from the source definition
    surv["id"] = sid                                     # authoritative new id
    surv["version"] = 1                                  # a copy starts its own version lineage at 1
    if title and title.strip():
        surv["title"] = title.strip()
    _defstore().put_definition({"survey": surv})
    _defstore().set_registry(sid, status="draft", note=f"copied from '{source_id}'")
    return {"ok": True, "id": sid}


def delete_survey(survey_id):
    """Remove a survey's definition + lifecycle (responses are kept in the response store)."""
    if not _defstore().get_definition(survey_id):
        raise ValueError(f"unknown survey {survey_id!r}")
    _defstore().delete_definition(survey_id)
    return {"ok": True, "id": survey_id}


def health():
    """Operator diagnostics — the resolved config + a quick survey tally (a `doctor` for the console)."""
    ov = overview()["vitals"]
    return {"definition_source": os.environ.get("SURVEY_DEFINITION_SOURCE", "file"),
            "response_store": (os.environ.get("SURVEY_STORE") or "memory"),
            "integration": os.environ.get("SURVEY_INTEGRATION", "none"),
            "output_dir": os.environ.get("SURVEY_OUTPUT_DIR", "reports"),
            "surveys_dir": os.environ.get("SURVEY_DIR", "surveys"),
            "public_base": _public_base(),
            "runtime": runtime_status(),
            "surveys": ov["surveys"], "live": ov["live"], "paused": ov["paused"], "completes": ov["completes"]}


def help_html():
    """Render the operator help — docs/CONSOLE.md if present (so it's an editable file), else the
    built-in copy — through the engine's safe Markdown renderer."""
    from survey_agent.render import md_to_html
    md = HELP_MD
    for p in (os.environ.get("SURVEY_CONSOLE_HELP"), os.path.join("docs", "CONSOLE.md")):
        if p and os.path.exists(p):
            try:
                md = open(p, encoding="utf-8").read()
                break
            except Exception:
                pass
    return md_to_html(md)


def dashboard_html():
    """The all-surveys management dashboard (a card per survey: params, live URL, quotas, counts, report
    link) — the same `survey-agent dashboard` report, served live + shareable as a link."""
    from survey_agent import dashboard, report as report_mod
    from survey_agent.engine.runtime import SurveyRuntime
    ds, store, base = _defstore(), _respstore(), _public_base().rstrip("/")
    cat = ds.list_surveys()
    live, report_links = {}, {}
    for e in cat:
        if e.get("missing_file"):
            continue
        sid = e["id"]
        spec = ds.get_definition(sid)
        if not spec:
            continue
        try:
            rt = SurveyRuntime(spec)
            rows, quotas = report_mod.collect_from_store(rt, store)
            rep = report_mod.build_report(rt, rows, quotas)
            live[sid] = {"params": [rt.respondent_var, rt.response_var], "url": f"{base}/s/{sid}",
                         "n": rep["n"], "completed": rep["funnel"]["counts"][0],
                         "incidence": rep["incidence"], "loi_median": (rep.get("loi") or {}).get("median_min"),
                         "quotas": rep.get("quotas") or []}
            report_links[sid] = f"/api/survey/{sid}/report"
        except Exception:
            pass
    return dashboard.render_html(cat, report_links=report_links, live=live,
                                 subtitle=f"{len([e for e in cat if not e.get('missing_file')])} survey(s) · "
                                          f"live counts from the {(os.environ.get('SURVEY_STORE') or 'memory')} store")


# ----------------------------------------------------------------- HTTP
class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else (json.dumps(body) if ctype == "application/json"
                                                      else str(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        if getattr(self, "_dl", None):
            self.send_header("Content-Disposition", f'attachment; filename="{self._dl}"')
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if path == "/":
            return self._send(200, _render_index(), "text/html")
        if path == "/api/overview":
            return self._send(200, overview())
        if path == "/api/templates":
            return self._send(200, template_catalog())
        if path == "/dashboard" or path == "/api/dashboard":
            return self._send(200, dashboard_html(), "text/html")
        if path == "/api/health":
            return self._send(200, health())
        if path == "/api/runtime":
            return self._send(200, runtime_status())
        if path == "/api/help":
            return self._send(200, help_html(), "text/html")
        if path.startswith("/api/template/"):
            d = template_detail(path.split("/api/template/", 1)[1])
            return self._send(200 if d else 404, d or {"error": "unknown template"})
        if path.startswith("/api/survey/"):
            rest = path.split("/api/survey/", 1)[1]
            sid, _, action = rest.partition("/")
            sid = urllib.parse.unquote(sid)
            if action == "report":
                html = report_html(sid, int(q.get("simulate") or 0))
                return self._send(200 if html else 404, html or "<p>unknown survey</p>", "text/html")
            if action == "insights":
                html = insights_html(sid, int(q.get("simulate") or 0))
                return self._send(200 if html else 404, html or "<p>unknown survey</p>", "text/html")
            if action == "validate":
                r = validate_survey(sid)
                return self._send(200 if r else 404, r or {"error": "unknown survey"})
            if action == "simulate":
                r = simulate_survey(sid, int(q.get("n") or 200))
                return self._send(200 if r else 404, r or {"error": "unknown survey"})
            if action == "responses":
                rows = _respstore().list_responses(sid)
                if q.get("format") == "csv":
                    self._dl = f"{sid}.csv"
                    return self._send(200, _csv(rows), "text/csv")
                self._dl = f"{sid}.json"
                return self._send(200, rows)
            d = survey_detail(sid)
            return self._send(200 if d else 404, d or {"error": "unknown survey"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        if u.path.startswith("/api/survey/") and u.path.endswith("/status"):
            sid = urllib.parse.unquote(u.path[len("/api/survey/"):-len("/status")])
            try:
                return self._send(200, set_status(sid, body.get("status"), approve=body.get("approve")))
            except LaunchGated as e:
                return self._send(403, {"error": str(e), "gated": True})
            except Exception as e:
                return self._send(400, {"error": str(e)})
        if u.path.startswith("/api/survey/") and u.path.endswith("/project"):
            sid = urllib.parse.unquote(u.path[len("/api/survey/"):-len("/project")])
            try:
                return self._send(200, set_project(sid, body.get("project")))
            except Exception as e:
                return self._send(400, {"error": str(e)})
        if u.path == "/api/new":
            try:
                return self._send(200, create_from_template(body.get("template"), body.get("id"), body.get("title")))
            except Exception as e:
                return self._send(400, {"error": str(e)})
        if u.path.startswith("/api/survey/") and u.path.endswith("/copy"):
            sid = urllib.parse.unquote(u.path[len("/api/survey/"):-len("/copy")])
            try:
                return self._send(200, copy_survey(sid, body.get("id"), body.get("title")))
            except Exception as e:
                return self._send(400, {"error": str(e)})
        if u.path.startswith("/api/survey/") and u.path.endswith("/delete"):
            sid = urllib.parse.unquote(u.path[len("/api/survey/"):-len("/delete")])
            try:
                return self._send(200, delete_survey(sid))
            except Exception as e:
                return self._send(400, {"error": str(e)})
        return self._send(404, {"error": "not found"})


def _csv(rows):
    import csv
    import io
    keys = ["id", "status", "date_submitted"]
    uvars = sorted({k for r in rows for k in (r.get("url_variables") or {})})
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(keys + [f"url.{k}" for k in uvars])
    for r in rows:
        w.writerow([r.get("id"), r.get("status"), r.get("date_submitted")]
                   + [(r.get("url_variables") or {}).get(k, "") for k in uvars])
    return buf.getvalue()


def _maybe_start_runtime():
    """Bring up a local respondent runtime so 'open as respondent' / live preview works the moment a
    survey is marked live. Only when the base URL is local: if something is already listening there we
    use it; otherwise we start an in-process multi-survey runtime (same definition store) on a daemon
    thread. Remote base URLs are left alone — you serve those yourself."""
    host, port, local = _runtime_target()
    if not local:
        print(f"[console] respondent runtime is remote ({_public_base().rstrip('/')}); preview links use it as-is.")
        return None
    if _port_open("127.0.0.1", port):
        print(f"[console] respondent runtime already up at {_public_base().rstrip('/')} — preview uses it.")
        return "external"
    os.environ["SURVEY_MULTI"] = "1"                      # read at server import time → serve EVERY survey at /s/<id>
    if not os.environ.get("SURVEY_PREVIEW_TOKEN"):        # mint a token so operator preview of pre-live surveys works
        import secrets
        os.environ["SURVEY_PREVIEW_TOKEN"] = secrets.token_urlsafe(16)
    try:
        import threading
        from survey_agent import server                   # imported AFTER SURVEY_MULTI is set
        threading.Thread(target=server.serve, args=(port,), kwargs={"host": "127.0.0.1"}, daemon=True).start()
        print(f"[console] respondent preview runtime on http://127.0.0.1:{port}  (multi · /s/<id>)")
        return "started"
    except Exception as e:
        print(f"[console] could not start the preview runtime ({e}); run `survey-agent serve --multi` yourself.")
        return None


def serve(port=8787, dir=None, open_browser=True, start_runtime=True, host=None):
    if dir:
        os.environ["SURVEY_DIR"] = dir
    from survey_agent import config
    config.apply_defaults()                                # same store/integration defaults as the CLI
    host = host or os.environ.get("SURVEY_CONSOLE_HOST") or "127.0.0.1"
    local = host in ("127.0.0.1", "localhost", "::1")
    if start_runtime:
        _maybe_start_runtime()
    httpd = ThreadingHTTPServer((host, port), _Handler)
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host       # a browsable URL for the operator
    url = f"http://{shown}:{port}"
    print(f"[console] operator console on http://{host}:{port}  (definition source: "
          f"{os.environ.get('SURVEY_DEFINITION_SOURCE', 'file')} · store: {(os.environ.get('SURVEY_STORE') or 'memory')})")
    if local:
        print("          local-only (127.0.0.1). Ctrl-C to stop.")
    else:
        print(f"          ⚠️  bound to {host} — the console is UNAUTHENTICATED and exposes operator controls")
        print("          (mark live, delete, status). Put it behind an auth proxy / VPN / trusted network;")
        print("          never expose it directly to the public internet. Ctrl-C to stop.")
    if open_browser and local:                                       # don't pop a browser on a headless host
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[console] stopped.")


HELP_MD = """# survey-agent — operator help

The console is a **read-first GUI onto the engine** — the same actions the agent runs, on a board.
Authoring stays with your agent; the console monitors, operates, and starts surveys from templates.

## In the console
- **Board** — every survey with a status light, completes, incidence, version. Click a row for detail.
- **Survey detail** — read-only preview (pages → questions → logic → terminals), the drop-in URL (copy),
  **open as respondent**, a **live report** (iframe), **download responses** (CSV), **Copy…** (duplicate
  into a new draft you rename and edit), and one-click **pause / resume / close / mark live**.
- **Templates** — browse the catalog and preview a template; **create a draft from one** (then refine it
  with your agent).
- **Tools** — quick actions: *New survey*, *Refresh*, *Health* (resolved config + a survey tally).
- **Refresh ↻** — the board is cached; it re-pulls only on refresh or after a lifecycle change.
- **Theme** — toggle dark / light (remembered).

## Lifecycle states
`draft` → `validated` → `tested` → `approved` → `live` → `paused` / `closed`. `paused` and `closed` are
enforced by the runtime (respondents see a closed page); the rest are operator labels.

## Commands your agent / CLI runs
**Author & shape** — `author` (draft from a brief), `edit`, `compile` / `render` (Markdown ⇄ JSON).
**Check** — `validate` (schema + consistency), `simulate -n 500` (coverage gate), `critique`
(methodology), `pretest` (incidence preview).
**Preview** — `serve --multi` (take it at `/s/<id>`), `convo` (take it as a chat), `console` (this UI).
**Launch & publish** — `launch` (drop-in URL + mark live), `publish` (write to the definition store —
live with no redeploy when a runtime store is configured), `status --set paused|closed|live`.
**Definitions** — `defs-export [--all]` (cold backup), `defs-import` (rebuild), `defs-diff` (reconcile).
**Report & data** — `report` (HTML), `insights` (AI read-out), `export` (CSV/Parquet), `dashboard`.
**Ops** — `list`, `status`, `doctor` (diagnose install/config).

## Configuration (env / survey-agent.config.yaml)
- `SURVEY_DEFINITION_SOURCE` — `file` (default) | `mongo` | plugin. Where definitions + lifecycle live.
- `SURVEY_STORE` — `memory` | `file` (default) | `sqlite` | `mongo` | `http` | plugin. Where responses/quotas live.
- `SURVEY_INTEGRATION` — `none` | `redirect` | `webhook` | `panel` | `mailchimp` | plugin. Terminal passback.
- `SURVEY_OUTPUT_DIR` — folder for generated reports/exports (default `reports/`).
- `SURVEY_PUBLIC_BASE_URL` — base for the drop-in survey URLs.

_Edit `docs/CONSOLE.md` to customize this help; the console renders that file when present._
"""


INDEX_HTML = r"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>survey-agent · console</title>
<style>
:root{
  --bg:#0a0c10; --bg2:#0e1116; --panel:#13171d; --panel2:#171c23; --line:#232b34; --line2:#2d3742;
  --ink:#e6edf3; --mut:#8a97a6; --dim:#5c6975; --acc:#46e0a0; --accdim:#1c6b4d;
  --live:#46e0a0; --paused:#f3b95c; --closed:#5c6975; --draft:#5aa9ff; --tested:#7c8cff; --bad:#e5675c;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
}
/* ---- themes (each just remaps the palette; midnight is the :root default) ---- */
[data-theme=light]{--bg:#f5f7fa;--bg2:#ffffff;--panel:#ffffff;--panel2:#eef2f7;--line:#e4e8ee;--line2:#d2dae2;--ink:#16202e;--mut:#55626e;--dim:#8893a0;--acc:#0e9e68;--accdim:#d4f3e6;--live:#0e9e68;--paused:#bd7a17;--closed:#9aa5b1;--draft:#2d6fe0;--tested:#5360d8;--bad:#d23f33}
[data-theme=dracula]{--bg:#282a36;--bg2:#21222c;--panel:#2b2e3b;--panel2:#343746;--line:#3a3d4d;--line2:#44485a;--ink:#f8f8f2;--mut:#9aa0b3;--dim:#6272a4;--acc:#bd93f9;--accdim:#3a2f5e;--live:#50fa7b;--paused:#f1fa8c;--closed:#6272a4;--draft:#8be9fd;--tested:#ff79c6;--bad:#ff5555}
[data-theme=nord]{--bg:#2e3440;--bg2:#2b303b;--panel:#3b4252;--panel2:#434c5e;--line:#434c5e;--line2:#4c566a;--ink:#eceff4;--mut:#a9b3c4;--dim:#7b88a1;--acc:#88c0d0;--accdim:#2e4a52;--live:#a3be8c;--paused:#ebcb8b;--closed:#4c566a;--draft:#81a1c1;--tested:#b48ead;--bad:#bf616a}
[data-theme=gruvbox]{--bg:#282828;--bg2:#1d2021;--panel:#32302f;--panel2:#3c3836;--line:#3c3836;--line2:#504945;--ink:#ebdbb2;--mut:#a89984;--dim:#7c6f64;--acc:#fabd2f;--accdim:#5a4a16;--live:#b8bb26;--paused:#fabd2f;--closed:#665c54;--draft:#83a598;--tested:#d3869b;--bad:#fb4934}
[data-theme=solarized]{--bg:#002b36;--bg2:#00252e;--panel:#073642;--panel2:#0a414f;--line:#0f4d5c;--line2:#14586a;--ink:#c9d6d6;--mut:#93a1a1;--dim:#586e75;--acc:#2aa198;--accdim:#11463f;--live:#859900;--paused:#b58900;--closed:#586e75;--draft:#268bd2;--tested:#6c71c4;--bad:#dc322f}
[data-theme=tokyonight]{--bg:#1a1b26;--bg2:#16161e;--panel:#1f2335;--panel2:#24283b;--line:#2a2e42;--line2:#353b52;--ink:#c0caf5;--mut:#9aa5ce;--dim:#565f89;--acc:#7aa2f7;--accdim:#24345e;--live:#9ece6a;--paused:#e0af68;--closed:#565f89;--draft:#7dcfff;--tested:#bb9af7;--bad:#f7768e}
[data-theme=onedark]{--bg:#282c34;--bg2:#21252b;--panel:#2c313a;--panel2:#333842;--line:#3a404b;--line2:#454c59;--ink:#d7dae0;--mut:#828997;--dim:#5c6370;--acc:#61afef;--accdim:#234055;--live:#98c379;--paused:#e5c07b;--closed:#5c6370;--draft:#61afef;--tested:#c678dd;--bad:#e06c75}
[data-theme=matrix]{--bg:#000800;--bg2:#001100;--panel:#001a00;--panel2:#002200;--line:#0a3a0a;--line2:#0f4f0f;--ink:#c8ffc8;--mut:#4fbf4f;--dim:#2a8f2a;--acc:#39ff14;--accdim:#0a3a0a;--live:#39ff14;--paused:#b6ff00;--closed:#2a8f2a;--draft:#00ff9c;--tested:#7dff7d;--bad:#ff3b3b}
[data-theme=amber]{--bg:#160d00;--bg2:#1a1000;--panel:#221600;--panel2:#2a1c00;--line:#3d2a08;--line2:#503810;--ink:#ffcf8f;--mut:#c89a4a;--dim:#8a6a2a;--acc:#ffb000;--accdim:#3d2a08;--live:#ffd166;--paused:#ffb000;--closed:#8a6a2a;--draft:#ffcf8f;--tested:#ffdd99;--bad:#ff5b3b}
.stamp{font:11px var(--mono);color:var(--dim);letter-spacing:.03em;white-space:nowrap;align-self:center}
.foot button{background:none;border:1px solid var(--line);color:var(--mut);border-radius:7px;padding:6px 10px;
  font:11px var(--mono);letter-spacing:.04em;cursor:pointer;width:100%;text-align:left;margin-bottom:7px}
.foot button:hover{color:var(--ink);border-color:var(--line2);background:var(--panel)}
*{box-sizing:border-box} html,body{margin:0;height:100%}
body{background-color:var(--bg);
   background-image:radial-gradient(1200px 600px at 80% -10%, color-mix(in srgb,var(--acc) 9%,transparent) 0%, transparent 60%),
   radial-gradient(900px 500px at -10% 110%, color-mix(in srgb,var(--ink) 5%,transparent) 0%, transparent 55%);
   color:var(--ink); font:14px/1.45 var(--sans); -webkit-font-smoothing:antialiased; display:flex; min-height:100vh; transition:background-color .25s,color .25s}
a{color:inherit} button{font:inherit;cursor:pointer}
.rail{width:208px;flex:0 0 208px;border-right:1px solid var(--line);background:linear-gradient(180deg,var(--bg2),var(--bg));
  padding:18px 14px;display:flex;flex-direction:column;gap:4px;position:sticky;top:0;height:100vh}
.brand{display:flex;align-items:center;gap:9px;padding:4px 8px 16px;font-family:var(--mono);font-weight:600;letter-spacing:.04em}
.brand .dot{width:9px;height:9px;border-radius:50%;background:var(--acc);box-shadow:0 0 10px var(--acc)}
.brand small{color:var(--dim);font-weight:400;font-size:10px;letter-spacing:.18em;text-transform:uppercase;display:block;margin-top:1px}
.nav{display:flex;flex-direction:column;gap:2px;margin-top:4px}
.nav button{display:flex;align-items:center;gap:9px;width:100%;text-align:left;background:none;border:0;color:var(--mut);
  padding:8px 10px;border-radius:8px;font-family:var(--mono);font-size:12.5px;letter-spacing:.02em}
.nav button:hover{color:var(--ink);background:var(--panel)}
.nav button.on{color:var(--ink);background:var(--panel2);box-shadow:inset 2px 0 0 var(--acc)}
.rail .foot{margin-top:auto;color:var(--dim);font-family:var(--mono);font-size:10.5px;letter-spacing:.04em;padding:8px}
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.top{display:flex;align-items:baseline;gap:18px;padding:20px 26px 14px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.top h1{font:600 16px var(--mono);letter-spacing:.03em;margin:0}
.top .sub{color:var(--dim);font-size:12px}
.vitals{display:flex;gap:10px;margin-left:auto;flex-wrap:wrap;align-items:center}
.btn.refresh{padding:9px 12px;font-size:15px;line-height:1;color:var(--mut)}
.btn.refresh:hover{color:var(--acc)}
.btn.refresh.spin{color:var(--acc);animation:throb 2.6s ease-in-out infinite}
.loading{color:var(--dim);font-family:var(--mono);padding:48px;text-align:center}
.loading::before{content:'';display:inline-block;width:13px;height:13px;margin-right:9px;vertical-align:-2px;
  border:2px solid var(--line2);border-top-color:var(--acc);border-radius:50%;animation:throb 2.6s ease-in-out infinite}
/* Netscape-throbber vibe: spin a couple of turns, then breathe/pulse, then loop (720deg==0 so no jump) */
@keyframes throb{
  0%{transform:rotate(0) scale(1);opacity:1;animation-timing-function:cubic-bezier(.45,.05,.55,.95)}
  42%{transform:rotate(720deg) scale(1);opacity:1;animation-timing-function:ease-in-out}
  57%{transform:rotate(720deg) scale(1.32);opacity:.45}
  72%{transform:rotate(720deg) scale(1);opacity:1}
  86%{transform:rotate(720deg) scale(1.18);opacity:.6}
  100%{transform:rotate(720deg) scale(1);opacity:1}}
.tile{border:1px solid var(--line);background:var(--panel);border-radius:10px;padding:8px 13px;min-width:78px}
.tile .n{font:600 19px var(--mono);letter-spacing:.02em;font-variant-numeric:tabular-nums}
.tile .k{color:var(--dim);font-size:10px;letter-spacing:.14em;text-transform:uppercase;margin-top:1px}
.tile.live .n{color:var(--live)} .tile.paused .n{color:var(--paused)}
.wrap{padding:18px 26px;overflow:auto}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:12px}
.search{flex:1;max-width:340px;background:var(--panel);border:1px solid var(--line);border-radius:9px;color:var(--ink);
  padding:8px 12px;font:13px var(--mono)}
.search::placeholder{color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:13px}
thead th{text-align:left;color:var(--dim);font:11px var(--mono);letter-spacing:.12em;text-transform:uppercase;
  padding:6px 12px;border-bottom:1px solid var(--line);font-weight:500}
tbody tr{border-bottom:1px solid var(--bg2)} tbody tr:hover{background:var(--panel)}
tbody td{padding:11px 12px;vertical-align:middle}
.row-t{cursor:pointer}
.title-c{font-weight:500} .title-c .id{display:block;color:var(--dim);font:11px var(--mono);margin-top:2px}
/* project groups (collapsible) */
.pgroup{margin-bottom:12px}
.phead{display:flex;align-items:center;gap:9px;padding:8px 11px;cursor:pointer;border-radius:8px;
  background:var(--panel);border:1px solid var(--line);user-select:none}
.phead:hover{border-color:var(--line2)} .phead .chev{color:var(--acc);font-size:11px;width:11px}
.phead .pname{font-weight:600;letter-spacing:.02em} .phead.closed .pname{color:var(--mut)}
.phead .pcount{margin-left:auto;font:11px var(--mono);color:var(--dim);background:var(--panel2);border-radius:999px;padding:1px 9px}
.pbody{margin-top:6px}
/* move-to-project bar in the detail drawer */
.projbar{display:flex;align-items:center;gap:8px;margin:8px 0}
.projbar .plabel{font:11px var(--mono);color:var(--dim);text-transform:uppercase;letter-spacing:.06em}
.projbar input{flex:0 1 220px;padding:6px 9px}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;text-align:right}
.badge{display:inline-flex;align-items:center;gap:7px;font:11px var(--mono);letter-spacing:.06em;text-transform:uppercase;
  padding:3px 9px;border-radius:999px;border:1px solid var(--line2);color:var(--mut);background:var(--panel)}
.badge .led{width:7px;height:7px;border-radius:50%;background:var(--closed)}
.s-live .led{background:var(--live);box-shadow:0 0 8px var(--live)} .s-live{color:var(--live);border-color:var(--accdim)}
.s-paused .led{background:var(--paused);box-shadow:0 0 8px var(--paused)} .s-paused{color:var(--paused)}
.s-closed .led{background:var(--closed)} .s-draft .led{background:var(--draft)} .s-draft{color:var(--draft)}
.s-tested .led,.s-validated .led{background:var(--tested)} .s-tested,.s-validated{color:var(--tested)}
.tag{display:inline-block;font:10.5px var(--mono);color:var(--dim);border:1px solid var(--line);border-radius:6px;padding:1px 6px;margin-right:4px}
.empty{color:var(--dim);padding:60px;text-align:center;font-family:var(--mono)}
/* checks (validate/simulate) + runtime note */
.checks{margin:8px 0 4px}
.chk{font:12px var(--mono);padding:7px 11px;border-radius:8px;border:1px solid var(--line2);background:var(--panel);margin:6px 0}
.chk.ok{color:var(--live);border-color:var(--accdim)} .chk.bad{color:var(--bad)} .chk.warn{color:var(--paused)}
.chk code{background:var(--panel2);padding:1px 5px;border-radius:4px}
.cl{font:11.5px var(--mono);padding:2px 11px;line-height:1.5} .cl.bad{color:var(--bad)} .cl.dim{color:var(--dim)}
a.btn.off{opacity:.5;text-decoration:line-through;cursor:not-allowed}
.uvtoggle{font:11px var(--mono);color:var(--dim);display:inline-flex;align-items:center;gap:5px;margin-left:8px;cursor:pointer;white-space:nowrap}
/* drawer */
.scrim{position:fixed;inset:0;background:rgba(4,6,9,.55);opacity:0;pointer-events:none;transition:.18s}
.scrim.on{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;height:100vh;width:min(640px,94vw);background:var(--bg2);border-left:1px solid var(--line2);
  transform:translateX(100%);transition:transform .22s cubic-bezier(.4,0,.2,1);display:flex;flex-direction:column;z-index:5;box-shadow:-30px 0 60px rgba(0,0,0,.4)}
.drawer.on{transform:none}
.dh{padding:18px 22px;border-bottom:1px solid var(--line)}
.dh .x{float:right;background:none;border:1px solid var(--line);color:var(--mut);border-radius:8px;width:30px;height:30px}
.dh h2{margin:0 0 3px;font:600 16px var(--sans)} .dh .id{color:var(--dim);font:11px var(--mono)}
.dmeta{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.dbody{padding:18px 22px;overflow:auto;flex:1}
.ops{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}
.btn{background:var(--panel2);border:1px solid var(--line2);color:var(--ink);padding:8px 13px;border-radius:8px;
  font:12.5px var(--mono);letter-spacing:.02em;display:inline-flex;align-items:center;gap:7px}
.btn:hover{border-color:var(--acc);color:var(--acc)} .btn.solid{background:var(--acc);border-color:var(--acc);color:var(--bg2)}
.btn.solid:hover{filter:brightness(1.08);color:var(--bg2)} .btn.danger:hover{border-color:var(--bad);color:var(--bad)}
.thememenu{position:fixed;left:12px;bottom:54px;width:188px;max-height:62vh;overflow:auto;background:var(--bg2);
  border:1px solid var(--line2);border-radius:11px;padding:6px;display:none;z-index:30;box-shadow:0 12px 44px rgba(0,0,0,.45)}
.thememenu.open{display:block}
.thememenu button{display:flex;align-items:center;gap:10px;width:100%;text-align:left;background:none;border:0;color:var(--mut);
  padding:7px 9px;border-radius:8px;font:12px var(--mono);cursor:pointer}
.thememenu button:hover{background:var(--panel);color:var(--ink)} .thememenu button.on{color:var(--ink)}
.thememenu button.on::after{content:'✓';margin-left:auto;color:var(--acc)}
.sw{width:26px;height:17px;border-radius:5px;border:1px solid rgba(128,128,128,.35);display:inline-flex;align-items:center;justify-content:flex-end;padding:2px;flex:0 0 auto}
.sw i{width:8px;height:8px;border-radius:50%;display:block}
.btn.warn:hover{border-color:var(--paused);color:var(--paused)} .btn.bad:hover{border-color:var(--bad);color:var(--bad)}
.urlbar{display:flex;gap:6px;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:8px;
  padding:7px 10px;font:11.5px var(--mono);color:var(--mut);margin-bottom:16px;overflow:hidden}
.urlbar code{color:var(--ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
.sec{margin:18px 0 8px;color:var(--dim);font:11px var(--mono);letter-spacing:.14em;text-transform:uppercase}
.page{border:1px solid var(--line);border-radius:10px;margin-bottom:10px;overflow:hidden}
.page>.pt{background:var(--panel);padding:7px 12px;font:11px var(--mono);color:var(--mut);letter-spacing:.06em;border-bottom:1px solid var(--line)}
.q{padding:10px 13px;border-bottom:1px solid var(--bg2)} .q:last-child{border:0}
.q .qt{font-weight:500} .q .qtype{font:10px var(--mono);color:var(--acc);text-transform:uppercase;letter-spacing:.1em;margin-right:7px}
.q .req{color:var(--bad)} .q .opt{color:var(--mut);font-size:12.5px;margin:3px 0 0 14px} .q .opt .ex{color:var(--paused)}
.term{display:inline-flex;align-items:center;gap:7px;font:11.5px var(--mono);border:1px solid var(--line);border-radius:7px;padding:4px 9px;margin:0 6px 6px 0;color:var(--mut)}
iframe.rep{width:100%;height:62vh;border:1px solid var(--line);border-radius:10px;background:#fff;margin-top:8px}
.repbar{display:flex;align-items:center;gap:10px;margin-top:8px}
.repbar .lab{font:12px var(--mono);padding:3px 10px;border-radius:999px;border:1px solid var(--line2)}
.repbar .lab.real{color:var(--live);border-color:var(--accdim)}
.repbar .lab.sim{color:#000;background:var(--paused);border-color:var(--paused);font-weight:600}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%) translateY(20px);background:var(--panel2);
  border:1px solid var(--line2);color:var(--ink);padding:10px 16px;border-radius:10px;font:12.5px var(--mono);opacity:0;
  transition:.2s;z-index:9} .toast.on{opacity:1;transform:translateX(-50%)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.card{border:1px solid var(--line);background:var(--panel);border-radius:12px;padding:14px;cursor:pointer}
.card:hover{border-color:var(--line2);background:var(--panel2)} .card h3{margin:0 0 4px;font:600 14px var(--sans)}
.card .src{font:10px var(--mono);color:var(--acc);text-transform:uppercase;letter-spacing:.1em}
.card p{color:var(--mut);font-size:12.5px;margin:7px 0 0}
.railsec{color:var(--dim);font:10px var(--mono);letter-spacing:.18em;text-transform:uppercase;padding:14px 10px 5px}
.help{max-width:780px;line-height:1.6} .help h1{font:600 20px var(--sans);margin:.2em 0 .5em}
.help h2{font:600 14px var(--mono);letter-spacing:.04em;color:var(--acc);text-transform:uppercase;margin:1.6em 0 .5em;border-top:1px solid var(--line);padding-top:1.1em}
.help h3{font:600 14px var(--sans);margin:1.1em 0 .3em} .help code{font:12px var(--mono);background:var(--panel);border:1px solid var(--line);border-radius:5px;padding:1px 5px}
.help ul{padding-left:18px} .help li{margin:4px 0} .help p{color:var(--mut)} .help strong{color:var(--ink)} .help em{color:var(--dim)}
.kv{width:100%;border-collapse:collapse} .kv td{padding:9px 10px;border-bottom:1px solid var(--bg2);font-size:13px}
.kv .kk{color:var(--dim);font:11px var(--mono);letter-spacing:.06em;text-transform:uppercase;width:42%} .kv .vv{font-family:var(--mono);color:var(--ink)}
.createbox{display:flex;flex-direction:column;gap:8px;max-width:360px;margin:4px 0}
</style></head><body>
<aside class=rail>
  <div class=brand><span class=dot></span><div>survey-agent<small>operator console</small></div></div>
  <div class=railsec>navigation</div>
  <nav class=nav id=nav>
    <button data-v=board class=on>▦ &nbsp;Board</button>
    <button data-v=templates>❏ &nbsp;Templates</button>
    <button data-v=help>?&nbsp;&nbsp;Help</button>
  </nav>
  <div class=railsec>tools</div>
  <nav class=nav id=tools>
    <button data-t=new>＋ &nbsp;New survey</button>
    <button data-t=dashboard>▤ &nbsp;Dashboard ↗</button>
    <button data-t=refresh>↻ &nbsp;Refresh</button>
    <button data-t=health>◉ &nbsp;Health</button>
  </nav>
  <div class=foot id=foot>
    <button id=theme title="theme">◐ &nbsp;theme</button>
    <div>local · 127.0.0.1</div>
  </div>
  <div class=thememenu id=thememenu></div>
</aside>
<main class=main>
  <div class=top>
    <div><h1 id=vtitle>Operations</h1><div class=sub id=vsub>every survey, live state, at a glance</div></div>
    <div class=vitals id=vitals></div>
  </div>
  <div class=wrap id=wrap></div>
</main>
<div class=scrim id=scrim></div>
<aside class=drawer id=drawer></aside>
<div class=toast id=toast></div>
<script>
const $=(s,e=document)=>e.querySelector(s), $$=(s,e=document)=>[...e.querySelectorAll(s)];
const api=async(p,o)=>{const r=await fetch(p,o);return r.headers.get('content-type','').includes('json')?r.json():r.text()};
const esc=s=>(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const pct=x=>x==null?'—':Math.round(x*100)+'%';
const rel=at=>{const s=Math.max(0,Math.round((Date.now()-at)/1000));return s<60?s+'s ago':s<3600?Math.round(s/60)+'m ago':Math.round(s/3600)+'h ago'};
let STATE={view:'board',data:null,templates:null,filter:'',at:0};
function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('on');clearTimeout(t._);t._=setTimeout(()=>t.classList.remove('on'),2200)}

function badge(st){return `<span class="badge s-${esc(st)}"><span class=led></span>${esc(st)}</span>`}

async function loadBoard(force){
  // cached: render from STATE.data on nav; only re-pull when forced (refresh / after a state change)
  if(force||!STATE.data){
    const r=$('#refresh');if(r)r.classList.add('spin');const s=$('#stamp');if(s)s.textContent='updating…';
    if(!STATE.data)$('#wrap').innerHTML='<div class=loading>loading surveys…</div>';
    STATE.data=await api('/api/overview');STATE.at=Date.now();
  }
  renderVitals();renderBoard();
}
function renderVitals(){
  const v=STATE.data?.vitals||{};
  $('#vitals').innerHTML=[['surveys',v.surveys,''],['live',v.live,'live'],['paused',v.paused,'paused'],['completes',v.completes,'']]
    .map(([k,n,c])=>`<div class="tile ${c}"><div class=n>${n??'—'}</div><div class=k>${k}</div></div>`).join('')
    +`<span class=stamp id=stamp>${STATE.at?'updated '+rel(STATE.at):''}</span>`
    +`<button class="btn refresh" id=refresh title="refresh">↻</button>`;
  $('#refresh').onclick=()=>{$('#refresh').textContent='…';loadBoard(true)};
}
function renderBoard(){
  // The toolbar + filter input render ONCE here; the filterable results live in #results and
  // re-render on their own (renderRows). Typing in #q therefore never destroys the input element,
  // so it keeps focus naturally — no focus()/setSelectionRange hack, no per-keystroke teardown.
  $('#wrap').innerHTML=`
   <div class=toolbar><input class=search id=q placeholder="filter surveys, projects, tags…" value="${esc(STATE.filter)}">
     <a class=btn href="/dashboard" target=_blank title="all-surveys dashboard (params · quotas · live links · counts)">▤ Dashboard ↗</a></div>
   <div id=results></div>`;
  const q=$('#q');if(q)q.oninput=e=>{STATE.filter=e.target.value.toLowerCase();renderRows()};
  renderRows();
}
function renderRows(){
  const rows=(STATE.data?.surveys||[]).filter(r=>!STATE.filter||
     (r.title+' '+r.id+' '+(r.tags||[]).join(' ')+' '+(r.project||'')).toLowerCase().includes(STATE.filter));
  const collapsed=JSON.parse(localStorage.getItem('sa_collapsed')||'{}');
  const head=`<thead><tr><th>Survey</th><th>State</th><th class=num>Ver</th><th class=num>Completes</th><th class=num>Incidence</th><th>Tags</th></tr></thead>`;
  const rowH=r=>`<tr class=row-t data-id="${esc(r.id)}">
       <td class=title-c>${esc(r.title)}<span class=id>${esc(r.id)}</span></td>
       <td>${badge(r.state)}</td><td class=num>v${esc(r.version||1)}</td>
       <td class=num>${r.completes}</td><td class=num>${pct(r.incidence)}</td>
       <td>${(r.tags||[]).slice(0,3).map(t=>`<span class=tag>${esc(t)}</span>`).join('')}</td></tr>`;
  // group by project — named groups A→Z, then "Ungrouped" last; collapsible (state remembered)
  const groups={}; rows.forEach(r=>{(groups[r.project||'']=groups[r.project||'']||[]).push(r)});
  const names=Object.keys(groups).sort((a,b)=>a===''?1:b===''?-1:a.localeCompare(b));
  const grouped = names.length>1 || (names.length===1 && names[0]!=='');
  let body;
  if(!rows.length){ body=`<div class=empty>${STATE.filter?'no surveys match':'no surveys in this store yet — create one from a template ❏'}</div>`; }
  else if(!grouped){ body=`<table>${head}<tbody>${rows.map(rowH).join('')}</tbody></table>`; }
  else { body=names.map(n=>{const open=!collapsed[n];return `<section class=pgroup>
       <div class="phead${open?'':' closed'}" data-proj="${esc(n)}"><span class=chev>${open?'▾':'▸'}</span>
         <span class=pname>${esc(n||'Ungrouped')}</span><span class=pcount>${groups[n].length}</span></div>
       <div class=pbody${open?'':' style="display:none"'}><table>${head}<tbody>${groups[n].map(rowH).join('')}</tbody></table></div>
     </section>`}).join(''); }
  $('#results').innerHTML=body;
  $$('.phead').forEach(h=>h.onclick=()=>{collapsed[h.dataset.proj]=!collapsed[h.dataset.proj];
    localStorage.setItem('sa_collapsed',JSON.stringify(collapsed));renderRows()});
  $$('.row-t').forEach(tr=>tr.onclick=()=>openSurvey(tr.dataset.id));
}

async function openSurvey(id){
  const drawer=$('#drawer');
  drawer.innerHTML=`<div class=dh><button class=x id=dx>✕</button><h2>${esc(id)}</h2><div class=id>loading…</div></div><div class=dbody><div class=loading>loading survey…</div></div>`;
  $('#scrim').classList.add('on');drawer.classList.add('on');$('#dx').onclick=$('#scrim').onclick=closeDrawer;
  const d=await api('/api/survey/'+encodeURIComponent(id));
  const PROJECTS=[...new Set((STATE.data?.surveys||[]).map(s=>s.project).filter(Boolean))].sort();
  const isLive=d.state==='live';
  const opsBtn=isLive
    ? `<button class="btn warn" data-st=paused>⏸ Pause</button><button class="btn bad" data-st=closed>■ Close</button>`
    : `<button class="btn solid" data-st=live>▶ Mark live</button>${d.state!=='closed'?'':'<button class="btn" data-st=live>Reopen</button>'}`;
  drawer.innerHTML=`
   <div class=dh><button class=x id=dx>✕</button>
     <h2>${esc(d.title||d.id)}</h2><div class=id>${esc(d.id)} · v${esc(d.version||1)}</div>
     <div class=dmeta>${badge(d.state)}<span class=tile style="padding:4px 10px"><b class=num>${d.completes}</b> <span class=k style="display:inline">completes</span></span>
       <span class=tile style="padding:4px 10px"><b class=num>${pct(d.incidence)}</b> <span class=k style="display:inline">incidence</span></span></div></div>
   <div class=dbody>
     <div class=ops>${opsBtn}
       <button class=btn id=val>✓ Validate</button>
       <button class=btn id=sim>🎲 Simulate</button>
       <button class=btn id=rep>📊 Report</button>
       <button class=btn id=ins>🧠 AI read-out</button>
       <a class=btn href="/api/survey/${encodeURIComponent(id)}/responses?format=csv">⬇ Responses</a>
       <a class=btn id=resp target=_blank href="${esc(d.preview_url||d.url)}" title="preview — collects no data">↗ As respondent</a>
       <button class=btn id=cpy title="duplicate into a new draft you can rename and edit">⧉ Copy…</button>
       <button class="btn danger" id=del>🗑 Delete</button></div>
     <div id=rtnote></div>
     <div id=chkmount></div>
     <div id=insmount></div>
     <div class=urlbar><code id=u>${esc(d.url)}</code><button class=btn id=cp>copy</button>
       ${(d.url_variables||[]).length?`<label class=uvtoggle title="append ?${esc((d.url_variables||[]).map(v=>v+'={'+v+'}').join('&'))}"><input type=checkbox id=uv> include captured params</label>`:''}</div>
     <div class=projbar><span class=plabel>📁 Project</span>
       <input class=search id=proj list=projlist value="${esc(d.project||'')}" placeholder="ungrouped" autocomplete=off>
       <datalist id=projlist>${PROJECTS.map(p=>`<option value="${esc(p)}"></option>`).join('')}</datalist>
       <button class=btn id=mv>Move</button></div>
     ${d.description?`<p style="color:var(--mut);font-size:13px;margin:0 0 6px">${esc(d.description)}</p>`:''}
     <div id=repmount></div>
     <div class=sec>Flow · ${d.pages.length} page(s)</div>
     ${d.pages.map((p,i)=>`<div class=page><div class=pt>PAGE ${i+1}${p.title?' · '+esc(p.title):''}${p.repeat?' · 🔁 per '+esc(p.repeat):''}</div>
        ${p.questions.map(q=>qHtml(q)).join('')||'<div class=q style="color:var(--dim)">— display only —</div>'}</div>`).join('')}
     <div class=sec>Terminals & logic</div>
     ${d.actions.map(a=>`<span class=term><span class=led style="background:${a.type==='complete'?'var(--live)':'var(--paused)'}"></span>
        ${esc(a.type)}${a.gated?' · conditional':''}${a.redirect?' → '+esc(a.redirect):''}</span>`).join('')||'<span style="color:var(--dim)">none</span>'}
     <div class=sec>URL variables captured</div>
     <div>${(d.url_variables||[]).map(v=>`<span class=tag>${esc(v)}</span>`).join('')||'<span style="color:var(--dim)">none</span>'}</div>
   </div>`;
  $('#scrim').classList.add('on');drawer.classList.add('on');
  $('#dx').onclick=$('#scrim').onclick=closeDrawer;
  const uvUrl=on=>(on&&(d.url_variables||[]).length)
    ? d.url+'?'+d.url_variables.map(v=>encodeURIComponent(v)+'={'+v+'}').join('&') : d.url;
  const uv=$('#uv');
  if(uv)uv.onchange=()=>{$('#u').textContent=uvUrl(uv.checked)};   // toggle the captured params in/out of the link
  $('#cp').onclick=()=>{navigator.clipboard.writeText(uvUrl(uv&&uv.checked));toast('URL copied')};
  $('#mv').onclick=async()=>{const p=$('#proj').value;
    const r=await api('/api/survey/'+encodeURIComponent(id)+'/project',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({project:p})});
    if(r.error){toast(r.error);return}
    toast(p.trim()?('moved to '+p.trim()):'ungrouped');STATE.data=null;closeDrawer();loadBoard(true)};
  $('#rep').onclick=async e=>{const t=e.target.textContent;e.target.textContent='loading…';
    renderReport(id,d.completes>0?0:200,d);   // default: REAL data if any responses exist, else a clearly-labelled preview
    e.target.textContent=t};
  runtimeNote(d);                                          // runtime reachable? + is this a preview (not live yet)?
  $('#val').onclick=async e=>{const t=e.target.textContent;e.target.textContent='…';e.target.disabled=true;
    const r=await api('/api/survey/'+encodeURIComponent(id)+'/validate');renderCheck(r,'validate',d);
    e.target.textContent=t;e.target.disabled=false};
  $('#sim').onclick=async e=>{const t=e.target.textContent;e.target.textContent='running 200…';e.target.disabled=true;
    $('#chkmount').innerHTML='<div class=chk style="color:var(--dim)">simulating 200 respondents…</div>';
    const r=await api('/api/survey/'+encodeURIComponent(id)+'/simulate?n=200');renderCheck(r,'simulate',d);
    e.target.textContent=t;e.target.disabled=false};
  $('#ins').onclick=async e=>{const t=e.target.textContent;e.target.textContent='thinking…';e.target.disabled=true;
    $('#insmount').innerHTML='<div class=chk style="color:var(--dim)">generating the AI read-out…</div>';
    // Read the REAL responses whenever any exist; only fall back to a (clearly-labelled) simulation
    // when there are none yet, so a read-out on a fielded survey always reflects real submissions.
    $('#insmount').innerHTML=await api('/api/survey/'+encodeURIComponent(id)+'/insights?simulate='+(d.completes>0?0:200));
    e.target.textContent=t;e.target.disabled=false};
  $$('[data-st]',drawer).forEach(b=>b.onclick=async()=>{
    const payload={status:b.dataset.st};
    if(b.dataset.st==='live'&&d.launch_gated){         // token-gated deployment: a human supplies the approval token
      const tok=prompt('Launch is gated here (SURVEY_LAUNCH_APPROVAL_TOKEN). Enter the approval token to mark "'+(d.title||id)+'" live:');
      if(tok===null||!tok.trim()){toast('not marked live — approval token required');return}
      payload.approve=tok.trim();
    }
    const label=b.textContent;b.disabled=true;b.textContent='…';
    const r=await api('/api/survey/'+encodeURIComponent(id)+'/status',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(payload)});
    if(r&&r.error){toast(r.error);b.disabled=false;b.textContent=label;return}
    toast('→ '+b.dataset.st);closeDrawer();loadBoard(true)});   // state changed -> force a refresh
  $('#cpy').onclick=async()=>{
    const name=prompt('Copy "'+(d.title||id)+'" to a new survey. New name (becomes the title; the id is derived from it):',(d.title||id)+' copy');
    if(name===null||!name.trim()){return}
    const r=await api('/api/survey/'+encodeURIComponent(id)+'/copy',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({id:name.trim(),title:name.trim()})});
    if(r.error){toast(r.error);return}
    toast('copied → '+r.id);STATE.data=null;await loadBoard(true);openSurvey(r.id);   // open the new draft to edit from there
  };
  $('#del').onclick=async()=>{
    if(prompt('Delete "'+id+'"? Its definition + lifecycle are removed (collected responses are kept). Type the id to confirm:')!==id){toast('cancelled');return}
    const r=await api('/api/survey/'+encodeURIComponent(id)+'/delete',{method:'POST'});
    if(r.error){toast(r.error);return}
    toast('deleted '+id);closeDrawer();STATE.data=null;loadBoard(true);
  };
}
function qHtml(q){
  return `<div class=q><div class=qt><span class=qtype>${esc(q.type)}</span>${esc(q.title)||'<i style="color:var(--dim)">untitled</i>'}${q.required?' <span class=req>*</span>':''}${q.has_logic?' <span class=tag>shown if…</span>':''}${q.repeat?' <span class=tag>🔁 per '+esc(q.repeat)+'</span>':''}</div>
    ${q.options.map(o=>`<div class=opt>○ ${esc(o.label)}${o.exclusive?' <span class=ex>· exclusive</span>':''}</div>`).join('')}
    ${q.rows.length?q.rows.map(r=>`<div class=opt>▭ ${esc(r)}</div>`).join(''):''}</div>`;
}
function renderReport(id,sim,d){
  // Report shows REAL submitted data by default; the operator can switch to a synthetic PREVIEW.
  // The banner always states which, since the report HTML itself can't tell real rows from simulated.
  const real=!sim;
  const lab=real
    ?`<span class="lab real">📊 Real responses${d.completes?(' · '+d.completes+' complete(s)'):' · none yet'}</span>`
    :`<span class="lab sim">🎲 Simulated preview — synthetic respondents, NOT real data</span>`;
  $('#repmount').innerHTML=`<div class=repbar>${lab}<button class=btn id=rmode>${real?'🎲 Show simulated preview':'📊 Show real data'}</button></div>`
    +`<iframe class=rep src="/api/survey/${encodeURIComponent(id)}/report?simulate=${real?0:200}"></iframe>`;
  $('#rmode').onclick=()=>renderReport(id,real?200:0,d);
}
function closeDrawer(){$('#scrim').classList.remove('on');$('#drawer').classList.remove('on')}
function renderCheck(r,kind){
  if(!r||r.error){$('#chkmount').innerHTML=`<div class="chk bad">${esc((r&&r.error)||'check failed')}</div>`;return}
  STATE.data=null;                                         // a passing check can advance lifecycle → board is stale
  const b=$('#drawer .badge');if(b&&r.state)b.outerHTML=badge(r.state);
  let h='';
  if(kind==='validate'){
    h = r.ok?`<div class="chk ok">✓ Valid — schema &amp; logic checks passed${r.warnings.length?` · ${r.warnings.length} warning(s)`:''}</div>`
            :`<div class="chk bad">✗ ${r.errors.length} error(s) — not launch-ready</div>`;
    h += (r.errors||[]).map(e=>`<div class="cl bad">• ${esc(e)}</div>`).join('');
    h += (r.warnings||[]).map(e=>`<div class="cl dim">• ${esc(e.replace(/^\[warn\]\s*/,''))}</div>`).join('');
  }else{
    const st=r.statuses||{},comp=st.completed||0,dq=st.disqualified||0;
    h = r.ok?`<div class="chk ok">✓ Coverage gate passed — every path reachable · n=${r.n}${r.loi_min?` · ~${r.loi_min} min`:''}</div>`
            :`<div class="chk bad">✗ ${r.problems.length} coverage gap(s) · n=${r.n}</div>`;
    h += (r.problems||[]).map(p=>`<div class="cl bad">• ${esc(p)}</div>`).join('');
    h += `<div class="cl dim">completed ${comp} · screened-out ${dq}`
       + (r.defined_reasons&&r.defined_reasons.length?` · screen-outs fired ${r.fired_reasons.length}/${r.defined_reasons.length}`:'')+`</div>`;
  }
  $('#chkmount').innerHTML=`<div class=checks>${h}</div>`;
}
function runtimeNote(d){
  const note=(d&&d.state!=='live')
    ? `<div class="chk warn">This survey is <b>${esc(d&&d.state||'draft')}</b> — respondents can't reach it yet. `
      +`<b>↗ As respondent</b> opens an operator <b>preview</b> (collects no data); <b>Mark live</b> opens it for real.</div>`
    : '';
  api('/api/runtime').then(rt=>{
    const resp=$('#resp');
    if(!rt.up){
      if(resp){resp.classList.add('off');resp.removeAttribute('target');resp.removeAttribute('href');
        resp.onclick=ev=>{ev.preventDefault();toast('no runtime serving '+rt.base)}}
      $('#rtnote').innerHTML=note+`<div class="chk warn">⚠ No respondent runtime is serving <b>${esc(rt.base)}</b>. `
        +`Respondents are served by a separate runtime — start one with <code>survey-agent serve --multi</code> `
        +`(or <code>survey-agent demo</code>).</div>`;
      return;
    }
    if(resp)resp.title='preview — collects no data · runtime '+rt.base;
    $('#rtnote').innerHTML=note;
  }).catch(()=>{$('#rtnote').innerHTML=note});
}

async function loadTemplates(force){
  if(force||!STATE.templates){$('#wrap').innerHTML='<div class=loading>loading templates…</div>';STATE.templates=await api('/api/templates')}   // catalog rarely changes; cache it
  const ts=STATE.templates;
  $('#wrap').innerHTML=`<div class=cards>${ts.map(t=>`<div class=card data-id="${esc(t.id)}">
     <div class=src>${esc(t.source||t.type||'core')}</div><h3>${esc(t.title||t.id)}</h3>
     <p>${esc(t.description||'')}</p>
     <div style="margin-top:9px">${(t.tags||[]).map(x=>`<span class=tag>${esc(x)}</span>`).join('')}</div></div>`).join('')}</div>`;
  $$('.card').forEach(c=>c.onclick=()=>openTemplate(c.dataset.id));
}
async function openTemplate(id){
  const t=await api('/api/template/'+encodeURIComponent(id));
  const drawer=$('#drawer');
  drawer.innerHTML=`<div class=dh><button class=x id=dx>✕</button><h2>${esc(t.title||t.id)}</h2>
     <div class=id>template · ${esc(t.source||t.type||'core')}</div>
     <div class=dmeta>${(t.tags||[]).map(x=>`<span class=tag>${esc(x)}</span>`).join('')}</div></div>
   <div class=dbody>
     ${t.description?`<p style="color:var(--mut)">${esc(t.description)}</p>`:''}
     <div class=sec>Create a draft from this template</div>
     <div class=createbox>
       <input class=search id=nid placeholder="survey id (e.g. ai-at-work)">
       <input class=search id=ntitle placeholder="title (optional)">
       <button class="btn solid" id=mk>＋ Create draft</button>
     </div>
     <p style="color:var(--dim);font:11.5px var(--mono);margin:6px 0 0">Scaffolds a draft in the store; refine &amp; validate it with your agent. Or in a terminal:</p>
     <div class=urlbar><code>survey-agent author "…" --template ${esc(t.id)}</code><button class=btn id=cp>copy</button></div>
     <div class=sec>Preview · ${(t.preview||[]).length} page(s)</div>
     ${(t.preview||[]).map((p,i)=>`<div class=page><div class=pt>PAGE ${i+1}${p.title?' · '+esc(p.title):''}${p.repeat?' · 🔁 per '+esc(p.repeat):''}</div>
        ${p.questions.map(q=>qHtml(q)).join('')||'<div class=q style="color:var(--dim)">— display only —</div>'}</div>`).join('')||'<div class=empty>no preview</div>'}
   </div>`;
  $('#scrim').classList.add('on');drawer.classList.add('on');
  $('#dx').onclick=$('#scrim').onclick=closeDrawer;
  $('#cp').onclick=()=>{navigator.clipboard.writeText('survey-agent author "..." --template '+id);toast('copied')};
  $('#mk').onclick=async()=>{
    const nid=$('#nid').value.trim(); if(!nid){toast('enter a survey id');return}
    const r=await api('/api/new',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({template:id,id:nid,title:$('#ntitle').value.trim()})});
    if(r.error){toast(r.error);return}
    toast('created draft '+r.id);closeDrawer();STATE.data=null;
    $('#nav button[data-v=board]').click();setTimeout(()=>openSurvey(r.id),120);
  };
}

async function loadHelp(){
  if(!STATE.help){$('#wrap').innerHTML='<div class=loading>loading help…</div>';STATE.help=await api('/api/help')}
  $('#wrap').innerHTML=`<div class=help>${STATE.help}</div>`;
}
async function loadHealth(){
  const drawer=$('#drawer');
  drawer.innerHTML=`<div class=dh><button class=x id=dx>✕</button><h2>Health</h2><div class=id>reading config…</div></div><div class=dbody><div class=loading>loading…</div></div>`;
  $('#scrim').classList.add('on');drawer.classList.add('on');$('#dx').onclick=$('#scrim').onclick=closeDrawer;
  const h=await api('/api/health');
  const rows=[['definition source',h.definition_source],['response store',h.response_store],['integration',h.integration],
    ['output dir',h.output_dir],['surveys dir',h.surveys_dir],['public base',h.public_base],
    ['surveys',h.surveys],['live',h.live],['paused',h.paused],['completes',h.completes]];
  drawer.innerHTML=`<div class=dh><button class=x id=dx>✕</button><h2>Health</h2><div class=id>resolved config + tally</div></div>
    <div class=dbody><table class=kv>${rows.map(([k,v])=>`<tr><td class=kk>${esc(k)}</td><td class=vv>${esc(v)}</td></tr>`).join('')}</table></div>`;
  $('#dx').onclick=$('#scrim').onclick=closeDrawer;
}
const VIEWS={board:['Operations','every survey, live state, at a glance',loadBoard],
  templates:['Templates','browse a template, then start a new survey',loadTemplates],
  help:['Help','commands & functions',loadHelp]};
$('#nav').onclick=e=>{const b=e.target.closest('button');if(!b)return;
  $$('#nav button').forEach(x=>x.classList.toggle('on',x===b));STATE.view=b.dataset.v;
  const [t,s,fn]=VIEWS[b.dataset.v];$('#vtitle').textContent=t;$('#vsub').textContent=s;
  $('#vitals').style.display=b.dataset.v==='board'?'':'none';fn();};
$('#tools').onclick=e=>{const b=e.target.closest('button');if(!b)return;
  if(b.dataset.t==='refresh'){loadBoard(true);toast('refreshed')}
  if(b.dataset.t==='new'){$('#nav button[data-v=templates]').click();toast('pick a template to start')}
  if(b.dataset.t==='dashboard'){window.open('/dashboard','_blank')}
  if(b.dataset.t==='health'){loadHealth()}};
// theme picker (persisted)
const THEME='sa.theme';
const THEMES=[{id:'midnight',name:'Midnight',bg:'#0e1116',acc:'#46e0a0'},{id:'light',name:'Light',bg:'#ffffff',acc:'#0e9e68'},
 {id:'dracula',name:'Dracula',bg:'#282a36',acc:'#bd93f9'},{id:'nord',name:'Nord',bg:'#2e3440',acc:'#88c0d0'},
 {id:'gruvbox',name:'Gruvbox',bg:'#282828',acc:'#fabd2f'},{id:'solarized',name:'Solarized',bg:'#002b36',acc:'#2aa198'},
 {id:'tokyonight',name:'Tokyo Night',bg:'#1a1b26',acc:'#7aa2f7'},{id:'onedark',name:'One Dark',bg:'#282c34',acc:'#61afef'},
 {id:'matrix',name:'Matrix',bg:'#000800',acc:'#39ff14'},{id:'amber',name:'Amber',bg:'#160d00',acc:'#ffb000'}];
const curTheme=()=>document.documentElement.dataset.theme||'midnight';
function renderThemeMenu(){$('#thememenu').innerHTML=THEMES.map(t=>`<button data-theme=${t.id} class="${t.id===curTheme()?'on':''}"><span class=sw style="background:${t.bg}"><i style="background:${t.acc}"></i></span>${t.name}</button>`).join('')}
function applyTheme(id){document.documentElement.dataset.theme=id;try{localStorage.setItem(THEME,id)}catch(e){};renderThemeMenu()}
applyTheme((()=>{try{return localStorage.getItem(THEME)}catch(e){}})()||(window.matchMedia&&matchMedia('(prefers-color-scheme: light)').matches?'light':'midnight'));
$('#theme').onclick=e=>{e.stopPropagation();$('#thememenu').classList.toggle('open')};
$('#thememenu').onclick=e=>{const b=e.target.closest('button');if(b){applyTheme(b.dataset.theme);$('#thememenu').classList.remove('open')}};
document.addEventListener('click',()=>$('#thememenu').classList.remove('open'));
setInterval(()=>{const s=$('#stamp');if(s&&STATE.at&&STATE.view==='board')s.textContent='updated '+rel(STATE.at)},5000);
document.onkeydown=e=>{if(e.key==='Escape')closeDrawer()};
loadBoard();
</script></body></html>"""
