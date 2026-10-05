"""Local survey runtime for the demo — the in-house replacement for a hosted survey_url.

Hosts a survey at  GET /s/{survey_id}?uid=&rid=&group=&lang=  (the exact drop-in URL shape),
captures ALL url params as url_variables, renders pages as themed HTML forms, runs the engine's
logic on each submit, fires the four panel passbacks over HTTP, and redirects on completion.

Pure stdlib + the engine/store/themes packages (jsonschema/pyyaml only). State lives in a
pluggable Store — `--store memory` (default), `--store file --data-dir ./survey_data`
(writes inspectable JSON sessions + JSONL responses), or mongo.

Run standalone (expects a mock receiver on :9000):
    python -m survey_agent.server          # or set SURVEY_FILE=... to serve a specific survey
Or `survey-agent demo` to start the runtime + a mock receiver together.
"""
import json
import os
import re
import signal
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from survey_agent.engine.runtime import SurveyRuntime, TERMINAL_STATUSES  # noqa: E402
from survey_agent.engine import export  # noqa: E402
import survey_agent.themes as themes  # noqa: E402
import survey_agent.store as store_pkg  # noqa: E402
import survey_agent.enrich as enrich  # noqa: E402
import survey_agent.integrations as integrations  # noqa: E402
import survey_agent.consent as consent  # noqa: E402
import survey_agent.ratelimit as ratelimit  # noqa: E402
from survey_agent import obs  # noqa: E402
from survey_agent.integrations import outbox as outbox_mod  # noqa: E402

log = obs.get_logger("survey_agent.runtime")

if __name__ == "__main__":                       # `python -m survey_agent.server` is an entry point:
    from survey_agent import config as _config   # same defaults as the CLI (file store, no integration)
    _config.apply_defaults()

PKG = os.path.dirname(os.path.abspath(__file__))
SURVEY_FILE = os.environ.get("SURVEY_FILE", os.path.join(PKG, "examples", "example.survey.json"))
from survey_agent import hosting as _hosting
PUBLIC_BASE = _hosting.public_base_url(default="http://127.0.0.1:8000")
ASSETS_DIR = os.environ.get("SURVEY_ASSETS_DIR", "assets")   # static files served at /assets/…
DEMO_QUOTA_LIMIT = os.environ.get("DEMO_QUOTA_LIMIT")
# Multi-survey mode (the hosted multi-survey shape): serve EVERY survey in SURVEY_DIR at /s/<id> from
# one process. Single-survey mode (default) keeps loading one SURVEY_FILE — that's the demo + tests.
MULTI = os.environ.get("SURVEY_MULTI", "").lower() in ("1", "true", "yes")
SURVEY_DIR = os.environ.get("SURVEY_DIR", "surveys")
# Data API: GET /api/responses[/<id>] hands stored responses (JSON/CSV) to whoever holds this shared
# secret. Unset ⇒ the endpoint is disabled — we never expose responses without a token.
API_TOKEN = os.environ.get("SURVEY_API_TOKEN")
URL_SECRET = os.environ.get("SURVEY_URL_SECRET")

WEBHOOK_URL = os.environ.get("SURVEY_WEBHOOK_URL", "http://127.0.0.1:9000/hook")   # only used by the webhook integration

# Opt-in per-IP rate limiter (SURVEY_RATE_LIMIT="60/min"); None ⇒ off. Applied to survey-serving
# paths only (page loads + submit), never to /health.
_RATE_LIMITER = ratelimit.from_env()

# Opt-in durable webhook outbox (SURVEY_OUTBOX=1); None ⇒ best-effort fire-once delivery.
_OUTBOX = outbox_mod.from_env()

# Set true while the process is draining on SIGTERM/SIGINT so /health returns 503 and the load
# balancer stops routing new traffic to us (in-flight requests still finish).
_SHUTTING_DOWN = False
# Cache the opt-in store-liveness probe so a chatty load balancer doesn't hammer the backend.
# A single (t, ok, detail) tuple, rebound atomically (GIL) so concurrent request threads never read a
# torn ok/detail pair; a bounded thundering-herd of pings every 10s is harmless.
_HEALTH = (0.0, True, "ok")


def _health_check():
    """(http_code, payload) for GET /health.

    Always cheap by default. Returns 503 while draining, and — when SURVEY_HEALTH_CHECK_STORE is set
    — 503 if a cached (10s TTL) store-liveness ping fails, so a bad DB connection string surfaces to
    the load balancer instead of silently failing every submit behind a green healthcheck."""
    global _HEALTH
    if _SHUTTING_DOWN:
        return 503, {"ok": False, "status": "shutting_down"}
    if (os.environ.get("SURVEY_HEALTH_CHECK_STORE") or "").lower() in ("1", "true", "yes"):
        t, ok, detail = _HEALTH
        if time.monotonic() - t > 10:
            from survey_agent import preflight
            try:
                preflight._ping_store(STORE)
                _HEALTH = (time.monotonic(), True, "ok")
            except Exception as e:  # noqa: BLE001
                _HEALTH = (time.monotonic(), False, str(e)[:200])
            _, ok, detail = _HEALTH
        if not ok:
            return 503, {"ok": False, "status": "store_unreachable", "detail": detail}
    return 200, {"ok": True}


def _limits_for(rt):
    return {q["id"]: (int(DEMO_QUOTA_LIMIT) if (DEMO_QUOTA_LIMIT and q.get("limit") is not None) else q.get("limit"))
            for q in rt.d.get("quotas", [])}


_RT_CACHE = {}


_DEFSTORE = None
def defstore():
    """The configured definition store (SURVEY_DEFINITION_SOURCE; default `file` = SURVEY_DIR +
    .registry.json). Lazy singleton so a deployment without a runtime backend needs no DB."""
    global _DEFSTORE
    if _DEFSTORE is None:
        from survey_agent import definitions
        _DEFSTORE = definitions.get_definition_store()
    return _DEFSTORE


def load_rt(survey_id):
    """Multi-survey mode: build (and cache) the SurveyRuntime for a survey id, via the definition store.
    Resolves the **live version** each new session (a cheap registry read) and caches by `(id, version)`
    — so a republish is picked up with no restart, while in-flight sessions keep their pinned version.
    Returns None if unknown."""
    if not survey_id:
        return None
    reg = defstore().get_registry(survey_id)
    version = reg.get("live_version") if reg else None
    key = (survey_id, version)
    if key in _RT_CACHE:
        return _RT_CACHE[key]
    spec = defstore().get_definition(survey_id, version)
    if spec is None:
        return None
    try:
        rt = SurveyRuntime(spec)
    except Exception:
        log.exception("failed to build runtime", extra={"survey_id": survey_id, "version": version})
        return None
    _RT_CACHE[key] = rt
    return rt


if MULTI:
    SPEC = RT = None
    VER, LIMITS, _SIGN_CFG, SIGN_REQUIRED, SIGN_FIELDS = 1, {}, {}, False, []
else:
    with open(SURVEY_FILE) as f:
        SPEC = json.load(f)
    RT = SurveyRuntime(SPEC)
    VER = RT.d.get("version", 1)
    LIMITS = _limits_for(RT)
    # Inbound URL-param signing (opt-in): enforced only when the survey sets settings.url_signing.required
    # AND a shared secret is configured (SURVEY_URL_SECRET). The engine ships the primitive
    # (engine/signing.py); the deployment decides whether eligibility params must be HMAC-signed.
    _SIGN_CFG = RT.d.get("settings", {}).get("url_signing") or {}
    SIGN_REQUIRED = bool(_SIGN_CFG.get("required"))
    SIGN_FIELDS = _SIGN_CFG.get("fields") or [RT.respondent_var, RT.response_var]
STORE = store_pkg.get_store()

_UPLOAD_STORE = None
def upload_store():
    """Lazily build the blob store for `file` answers (SURVEY_UPLOAD_STORE; default local). Lazy so a
    survey with no file questions never needs S3 creds configured."""
    global _UPLOAD_STORE
    if _UPLOAD_STORE is None:
        from survey_agent.store import uploads as _uploads
        _UPLOAD_STORE = _uploads.get_upload_store()
    return _UPLOAD_STORE


def parse_post(headers, body):
    """(fields, files) from a POST body. fields={name:[str,...]} like parse_qs; files={name:[(filename,
    content_type, bytes),...]}. Handles urlencoded and multipart/form-data via the stdlib email parser
    (the `cgi` module was removed in 3.13)."""
    ctype = headers.get("Content-Type", "")
    if not ctype.startswith("multipart/form-data"):
        return parse_qs(body.decode("utf-8", "replace")), {}
    import email
    msg = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body)
    fields, files = {}, {}
    for part in msg.walk():
        if part.is_multipart():
            continue
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_param("filename", header="content-disposition")
        payload = part.get_payload(decode=True) or b""
        if filename:                                  # a chosen file (empty filename = no file picked)
            files.setdefault(name, []).append((filename, part.get_content_type(), payload))
        else:
            fields.setdefault(name, []).append(payload.decode("utf-8", "replace"))
    return fields, files


# Passback delivery. Unset ⇒ `none` (no outbound calls). `survey-agent demo` explicitly selects the
# webhook integration pointed at its mock receiver; a deployment picks none|redirect|webhook|panel or
# a plugin via SURVEY_INTEGRATION.
from survey_agent import env_select as _env_select  # noqa: E402
_INT_KIND = _env_select("SURVEY_INTEGRATION") or "none"
# only the webhook integration takes the demo receiver URL; redirect/panel/etc. read their own env.
INTEGRATION = (integrations.get_integration(_INT_KIND, url=WEBHOOK_URL) if _INT_KIND == "webhook"
               else integrations.get_integration(_INT_KIND))
# Per-respondent behavioral fixtures so per-user gating is real (some respondents have a search and
# get the "why" page; others don't and skip it). Stands in for a warehouse lookup by the respondent id.
_contrib_path = os.path.join(PKG, "enrichments", "_demo_respondents.json")
if os.path.exists(_contrib_path):
    _by_uid = {k: v for k, v in json.load(open(_contrib_path)).items() if not k.startswith("_comment")}
    ENRICH = enrich.StaticProvider(by_uid=_by_uid)
else:
    ENRICH = enrich.get_provider()
_PERSONAS = [
    ("1001", "Maya — searched Netflix recently → will be asked WHY", True),
    ("1003", "Priya — searched streaming once → asked why", True),
    ("1002", "Tom — no streaming search → behavior question is skipped", False),
]
_demo_counter = {"n": 1000}
# A per-process token so demo links are fresh on every run: with a persistent store, a fixed rid would
# collide with the previous run's finished session and show "Already done".
import secrets as _secrets  # noqa: E402
_RUN_TAG = _secrets.token_hex(3)


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _capture_client_meta():
    """Network metadata (IP / user-agent / a client-JS fingerprint) is PII and is OFF by default in
    the open core. A deployer opts in with SURVEY_CAPTURE_CLIENT_META=1 to record per-response
    fraud/bot signals; they then ride INSIDE the response object, so existing data-export / DSAR-erasure
    tooling covers the IP automatically. See docs/DATA_SECURITY.md before enabling."""
    return os.environ.get("SURVEY_CAPTURE_CLIENT_META", "").strip().lower() in ("1", "true", "yes")


# Network-level metadata captured per response when SURVEY_CAPTURE_CLIENT_META is enabled. These are
# fraud/bot signals — fraud handling proper lives elsewhere, but we record what we reasonably can
# alongside the answers. IP is PII; it rides inside the response object so existing data-export /
# DSAR-erasure tooling covers it automatically.
def client_meta(headers, client_address):
    """Server-side request metadata for a new session. Behind an ALB/proxy the real client IP is the
    LEFT-most `X-Forwarded-For` entry (the socket peer is the load balancer); fall back to the peer."""
    xff = headers.get("X-Forwarded-For", "") or ""
    chain = [p.strip() for p in xff.split(",") if p.strip()]
    peer = client_address[0] if client_address else None
    meta = {
        "ip": chain[0] if chain else peer,
        "user_agent": headers.get("User-Agent"),
        "accept_language": headers.get("Accept-Language"),
        "referer": headers.get("Referer"),
        "forwarded_proto": headers.get("X-Forwarded-Proto"),
    }
    if len(chain) > 1:                              # multiple hops → keep the chain for proxy/VPN review
        meta["ip_chain"] = chain
    return {k: v for k, v in meta.items() if v}


_FP_MAX = 8000   # cap stored fingerprint payload — it's untrusted client input


def _parse_fingerprint(raw):
    """Best-effort parse of the client-side `__fp` field (a JSON object). Falls back to a truncated
    raw string if it isn't valid JSON, and is always size-capped (untrusted input)."""
    raw = (raw or "")[:_FP_MAX]
    if not raw:
        return None
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"raw": str(val)[:_FP_MAX]}
    except Exception:
        return {"raw": raw}


# Dependency-free client fingerprint. Collects a compact, stable signal bundle + a quick hash for
# bucketing. Injected once (first page); the value posts back in the hidden `__fp` field on submit.
FINGERPRINT_JS = """<script>(function(){try{
var n=navigator,s=screen;function h(str){var a=0,i;for(i=0;i<str.length;i++){a=((a<<5)-a+str.charCodeAt(i))|0;}return (a>>>0).toString(16);}
var canvas="";try{var c=document.createElement('canvas'),x=c.getContext('2d');
x.textBaseline='top';x.font="14px 'Arial'";x.fillStyle='#f60';x.fillRect(125,1,62,20);
x.fillStyle='#069';x.fillText('survey-agent \\u26A1',2,15);canvas=h(c.toDataURL());}catch(e){}
var fp={ua:n.userAgent,lang:n.language||"",langs:(n.languages||[]).join(","),
platform:n.platform||"",vendor:n.vendor||"",
tz:((Intl&&Intl.DateTimeFormat)?Intl.DateTimeFormat().resolvedOptions().timeZone:"")||"",
tzo:new Date().getTimezoneOffset(),screen:s.width+"x"+s.height+"x"+s.colorDepth,
dpr:window.devicePixelRatio||1,cores:n.hardwareConcurrency||0,mem:n.deviceMemory||0,
touch:(('ontouchstart' in window)||n.maxTouchPoints>0),cookie:!!n.cookieEnabled,
dnt:n.doNotTrack||"",canvas:canvas};
fp.hash=h(JSON.stringify(fp));
var el=document.getElementById('__fp');if(el)el.value=JSON.stringify(fp);
}catch(e){}})();</script>"""


def _signature_ok(query, rt=None):
    """Is this inbound link's signature acceptable? True unless the survey requires signing AND a
    secret is configured AND the `sig` param fails to verify — so signing is opt-in and a forged
    uid/rid/group can't create a session. Verification itself is the pure engine primitive."""
    rt = rt or RT
    cfg = (rt.d.get("settings", {}).get("url_signing") or {})
    required = bool(cfg.get("required"))
    fields = cfg.get("fields") or [rt.respondent_var, rt.response_var]
    if not (required and URL_SECRET):
        return True
    from survey_agent.engine import signing
    return signing.verify(query, URL_SECRET, fields)


def quota_state(rt=None):
    rt = rt or RT
    ver = rt.d.get("version", 1)
    limits = _limits_for(rt)
    counts = STORE.quota_counts(rt.d["id"], ver)
    return {qid: {"count": counts.get(qid, 0), "limit": limits[qid]} for qid in limits}


# passbacks are delivered by the integration adapter (INTEGRATION). The demo's mock receiver stands in for your panel.


def _mint_response_id():
    """A unique, system-generated id for one response — minted at session start (so even abandoned/
    partial sessions are addressable) and used as the stored response id + terminal/passback
    idempotency key. Independent of the inbound identity vars (uid/rid), which stay as data free for
    dedup/eligibility checks. The resume key (survey:rid:uid) is unchanged; only the response identity
    moves here."""
    import uuid
    return uuid.uuid4().hex


def on_terminal(session, terminal, rt=None):
    rt = rt or RT
    if session.get("preview"):                  # operator preview: walk the flow, but record/passback nothing
        session["date_submitted"] = now_iso()
        STORE.save_session(session)
        return
    ver = rt.d.get("version", 1)
    limits = _limits_for(rt)
    if terminal["status"] == "completed":
        counts = STORE.quota_counts(rt.d["id"], ver)
        for qid in rt.quota_buckets(session):
            lim = limits.get(qid)
            if lim is None or counts.get(qid, 0) < lim:
                STORE.incr_quota(rt.d["id"], ver, qid)
    # opt-in, audited LLM analysis on the respondent's answers (fail-open). Lands in the response
    # (derived + llm_audit) and can be piped to screen.
    try:
        vals, audit = enrich.resolve_llm_for_session(rt.d, session, now=now_iso())
        session.setdefault("enrichments", {}).update(vals)
        if audit:
            session["llm_audit"] = audit
    except Exception:
        pass
    session["date_submitted"] = now_iso()
    STORE.save_session(session)
    STORE.record_response(rt.d["id"], export.response_from_session(
        rt, session, session.get("date_started"), session.get("date_submitted")))
    INTEGRATION.terminal(session, terminal["status"], reason_code=terminal.get("reason_code"))


# ----------------------------------------------------------------- HTML
def _html(body, title="survey-agent", theme=None):
    css = themes.load_css(theme or (RT.d.get("settings", {}).get("theme") if RT else None) or _default_theme())
    js = ("<script>function rmove(b,d){var li=b.closest('li'),"
          "s=d<0?li.previousElementSibling:li.nextElementSibling;"
          "if(s){d<0?li.parentNode.insertBefore(li,s):li.parentNode.insertBefore(s,li);}}"
          # exclusive checkbox ("none of the above"): checking it clears + disables the rest of its group
          # (and re-enables on uncheck); checking any normal option clears the exclusive one.
          "function sgX(g,on){for(var i=0;i<g.length;i++){var c=g[i];if(!c.dataset.exclusive){if(on)c.checked=false;c.disabled=on;}}}"
          "document.addEventListener('change',function(e){var t=e.target;if(t.type!=='checkbox'||!t.name)return;"
          "var g=document.getElementsByName(t.name);"
          "if(t.dataset.exclusive){sgX(g,t.checked);}"
          "else if(t.checked){for(var i=0;i<g.length;i++){if(g[i].dataset.exclusive)g[i].checked=false;}}});"
          "document.addEventListener('DOMContentLoaded',function(){"
          "var x=document.querySelectorAll('input[type=checkbox][data-exclusive]:checked');"
          "for(var i=0;i<x.length;i++){sgX(document.getElementsByName(x[i].name),true);}});</script>")
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{escape(title)}</title><style>{css} .grid td,.grid th{{border:1px solid var(--border,#ddd);padding:8px}} "
            f".grid-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch}} .grid th,.grid td{{white-space:nowrap}} "
            f".grid th:first-child,.grid td:first-child{{white-space:normal;position:sticky;left:0;background:var(--ms-surface,#fff)}} "
            f".mini{{padding:6px 12px;font-size:16px;margin-left:4px}} .q .desc{{color:var(--muted,#666);margin:.2em 0 .6em}} "
            f"blockquote{{border-left:3px solid var(--border,#ddd);margin:.6em 0;padding:.2em .9em;color:var(--muted,#666)}} "
            f"code{{background:rgba(0,0,0,.06);padding:1px 5px;border-radius:4px;font-size:.92em}} "
            f".q img{{max-width:100%}} "
            f"@media(max-width:480px){{body{{padding:28px 16px}} .q{{padding:16px 15px}} form>button{{width:100%}}}}"
            f"</style><div class=brandbar></div>{body}{js}")


def _video_html(media):
    from survey_agent.media import youtube_embed
    url = media.get("url", "")
    cap = f'<div class="meta">{escape(media.get("caption"))}</div>' if media.get("caption") else ""
    embed = youtube_embed(url)
    if embed:
        return (f'<div style="position:relative;padding-top:56.25%"><iframe src="{escape(embed)}" '
                f'style="position:absolute;top:0;left:0;width:100%;height:100%;border:0;border-radius:8px" '
                f'allow="accelerometer;autoplay;encrypted-media;picture-in-picture" allowfullscreen></iframe></div>{cap}')
    return f'<video controls style="width:100%;border-radius:8px" src="{escape(url)}"></video>{cap}'


# image + Markdown rendering live in the shared, side-effect-free `render` module so the Django
# reference service reuses the exact same safe implementation.
from survey_agent.render import image_html as _image_html, md_to_html as _md_to_html, md_inline as _md_inline  # noqa: E402


def _survey_state(survey_id):
    """Operator lifecycle state from the definition store's registry. The runtime **enforces** the
    lifecycle: only states in SURVEY_SERVE_STATES (default `live`) serve real respondents; `paused`/
    `closed` get a closed page; everything else (draft/validated/tested/approved) is 'not open yet'
    unless it's an operator preview. Returns None if unknown / no registry."""
    try:
        reg = defstore().get_registry(survey_id)
        return reg.get("status") if reg else None
    except Exception:
        return None


def _serve_states():
    """Lifecycle states the runtime serves to real respondents. Default: just `live` — so 'live' means
    deployed + collecting. Operators can widen this during a migration (SURVEY_SERVE_STATES=live,approved)
    and tighten back once everything's been marked live."""
    raw = os.environ.get("SURVEY_SERVE_STATES", "live")
    return {s.strip() for s in raw.split(",") if s.strip()}


def _serve_all_states():
    """Local sandbox mode (`survey-agent demo`, `serve --preview`): every lifecycle state is served
    (drafts included) and collected normally. Selected with SURVEY_SERVE_STATES=* — never by --public."""
    return "*" in _serve_states()


def _preview_ok(query):
    """Operator preview bypass for not-yet-open surveys. Gated by SURVEY_PREVIEW_TOKEN: the request
    must carry `preview=<token>`. With no token configured the gate can't be bypassed (safe default).
    Preview sessions render the survey but collect nothing and fire no passback (see on_terminal)."""
    tok = os.environ.get("SURVEY_PREVIEW_TOKEN")
    return bool(tok) and query.get("preview") == tok


def _closed_page(state, theme=None):
    head, msg = (("Closed", "This survey is closed. Thank you for your interest.") if state == "closed"
                 else ("Paused", "This survey is paused right now — please check back later."))
    return _html(f"<div class='term'><h1>{escape(head)}</h1><p>{escape(msg)}</p></div>", theme=theme)


def _not_open_page(theme=None):
    """Shown when a respondent hits a survey that hasn't been opened (anything not in SURVEY_SERVE_STATES,
    e.g. a draft). Operators preview such surveys with a preview token; respondents see this."""
    return _html("<div class='term'><h1>Not open yet</h1><p>This survey isn't open for responses right "
                 "now. Please use the link you were sent once it goes live.</p></div>", theme=theme)


def render_question(q, errors):
    t = q["type"]
    # display-only image stimulus gets a frameless, white, shrink-wrapped block (no question-card
    # chrome) so full-bleed slide images blend into the page instead of looking like a question.
    cls = "q media" if t == "image" else "q"
    parts = [f'<div class="{cls}">']
    title = q.get("title_md") or q.get("title")     # *_md: piped values made Markdown-safe by the engine
    if title:
        # Titles render the same safe inline-markdown subset as bodies (bold/italic/links/code/img),
        # so emphasis and links in a prompt come through instead of showing literal `**`/`[...](...)`.
        parts.append(f'<div class="title">{_md_inline(title)}'
                     + (' <span class="meta">*</span>' if q.get("required") else "") + "</div>")
    if q.get("body") and t != "instructions":          # rich description under any question's title
        parts.append(f'<div class="desc">{_md_to_html(q.get("body_md") or q["body"])}</div>')
    cur = q.get("answer")
    if t in ("radio", "rating", "nps"):
        opts = q.get("options") or [{"value": p, "label": str(p)} for p in q.get("scale_points", [])]
        for o in opts:
            checked = "checked" if str(cur) == str(o["value"]) else ""
            parts.append(f'<label><input type=radio name="{q["id"]}" value="{escape(str(o["value"]))}" {checked}> {_md_inline(str(o.get("label_md") or o["label"]))}</label>')
    elif t == "checkbox":
        cur = cur if isinstance(cur, list) else ([cur] if cur else [])
        for o in q.get("options", []):
            checked = "checked" if str(o["value"]) in set(map(str, cur)) else ""
            excl = " data-exclusive=1" if o.get("exclusive") else ""   # "none of the above" clears the rest
            parts.append(f'<label><input type=checkbox name="{q["id"]}" value="{escape(str(o["value"]))}" {checked}{excl}> {_md_inline(str(o.get("label_md") or o["label"]))}</label>')
    elif t in ("dropdown", "dropdown_multi"):
        multi = t == "dropdown_multi"
        sel = set(map(str, cur)) if isinstance(cur, list) else {str(cur)}
        # <option> can't render HTML, so dropdown labels stay plain-escaped (no inline markdown here).
        opts = "".join(f'<option value="{escape(str(o["value"]))}" {"selected" if str(o["value"]) in sel else ""}>{escape(str(o["label"]))}</option>'
                       for o in q.get("options", []))
        placeholder = "" if multi else '<option value="">— choose —</option>'
        parts.append(f'<select name="{q["id"]}" {"multiple size=4" if multi else ""}>{placeholder}{opts}</select>')
    elif t == "number":
        v = q.get("validation", {})
        parts.append(f'<input type=number inputmode="decimal" name="{q["id"]}" value="{escape(str(cur)) if cur is not None else ""}" '
                     f'{"min="+str(v["min"]) if "min" in v else ""} {"max="+str(v["max"]) if "max" in v else ""}>')
    elif t == "textarea":
        parts.append(f'<textarea name="{q["id"]}" rows=4>{escape(str(cur)) if cur else ""}</textarea>')
    elif t == "ranking":
        opts = q.get("options", [])
        bylabel = {str(o["value"]): o.get("label_md") or o.get("label", o["value"]) for o in opts}
        order = [str(v) for v in cur if str(v) in bylabel] if isinstance(cur, list) else []
        order += [str(o["value"]) for o in opts if str(o["value"]) not in order]   # leftovers keep author order
        parts.append(f'<ol class="rank" style="padding-left:1.4em">')
        for val in order:
            parts.append(f'<li style="margin:4px 0"><input type=hidden name="{q["id"]}" value="{escape(val)}">'
                         f'{_md_inline(str(bylabel[val]))} '
                         f'<button type=button class="mini" onclick="rmove(this,-1)">▲</button>'
                         f'<button type=button class="mini" onclick="rmove(this,1)">▼</button></li>')
        parts.append("</ol>")
    elif t == "grid":
        cols = q.get("options", [])
        ctype = "checkbox" if q.get("cell_type") == "checkbox" else "radio"
        cur = cur if isinstance(cur, dict) else {}
        head = "".join(f"<th class='meta'>{_md_inline(str(c.get('label_md') or c.get('label', c['value'])))}</th>"
                       for c in cols)
        parts.append('<div class="grid-wrap">'
                     f'<table class="grid" style="min-width:100%;border-collapse:collapse"><tr><th></th>{head}</tr>')
        for r in q.get("rows", []):
            rid = r["id"]
            picked = set(map(str, cur.get(rid))) if isinstance(cur.get(rid), list) else {str(cur.get(rid))}
            cells = "".join(
                f'<td style="text-align:center"><input type={ctype} name="{q["id"]}::{rid}" '
                f'value="{escape(str(c["value"]))}" {"checked" if str(c["value"]) in picked else ""}></td>'
                for c in cols)
            parts.append(f'<tr><td>{_md_inline(str(r.get("label_md") or r.get("label", rid)))}</td>{cells}</tr>')
        parts.append("</table></div>")
    elif t == "video":
        parts.append(_video_html(q.get("media", {})))
    elif t == "image":
        parts.append(_image_html(q.get("media", {})))
    elif t == "instructions":
        parts.append(_md_to_html(q.get("body_md") or q.get("body", "")))   # a formatted how-to / task-instructions page
    elif t == "date":
        v = q.get("validation", {})
        bounds = (f' min="{escape(str(v["min"]))}"' if v.get("min") else "") + \
                 (f' max="{escape(str(v["max"]))}"' if v.get("max") else "")
        parts.append(f'<input type=date name="{q["id"]}" value="{escape(str(cur)) if cur else ""}"{bounds}>')
    elif t == "text":
        v = q.get("validation", {})
        fmt = v.get("format")
        itype = "tel" if fmt == "phone" else "email" if fmt == "email" else "text"
        # right mobile keyboard + autofill for typed identity fields
        extra = (' inputmode="tel" autocomplete="tel"' if fmt == "phone"
                 else ' inputmode="email" autocomplete="email"' if fmt == "email" else "")
        ml = f' maxlength="{v["max_length"]}"' if v.get("max_length") else ""
        parts.append(f'<input type={itype}{extra} name="{q["id"]}" value="{escape(str(cur)) if cur else ""}"{ml}>')
    elif t == "file":
        v = q.get("validation", {})
        exts = v.get("extensions") or []
        accept = (' accept="' + ",".join("." + e.lstrip(".") for e in exts) + '"') if exts else ""
        multiple = " multiple" if (v.get("max_files") or 1) > 1 else ""
        parts.append(f'<input type=file name="{q["id"]}"{accept}{multiple}>')
        if isinstance(cur, dict) and cur.get("filename"):           # show what's already attached on re-render
            parts.append(f'<div class="meta">Attached: {escape(str(cur["filename"]))}</div>')
        hint = []
        if exts:
            hint.append("Allowed: " + ", ".join("." + e.lstrip(".") for e in exts))
        if v.get("max_size_kb"):
            hint.append(f"max {v['max_size_kb']} KB")
        if hint:
            parts.append(f'<div class="meta">{escape(" · ".join(hint))}</div>')
    else:
        parts.append(f'<input type=text name="{q["id"]}" value="{escape(str(cur)) if cur else ""}">')
    if q["id"] in errors:
        parts.append(f'<div class="err">{escape(errors[q["id"]])}</div>')
    parts.append("</div>")
    return "".join(parts)


def _default_theme():
    """Deployment-wide default theme (SURVEY_THEME, or `theme:` in survey-agent.config.yaml) for surveys
    that don't set settings.theme. None ⇒ the built-in default."""
    return os.environ.get("SURVEY_THEME") or None


def session_theme(session, rt=None):
    """The theme chosen for this session (carried from the landing page), else the survey default,
    else the deployment default."""
    rt = rt or RT
    return session.get("theme") or rt.d.get("settings", {}).get("theme") or _default_theme()


def _show_title(rt):
    """Whether to render the survey title as an <h1> banner on each page. A per-survey
    settings.show_title (bool) wins; otherwise the deployment default SURVEY_SHOW_TITLE (default
    true). Set it false to drop the repeated title banner when the page content already carries the
    heading — e.g. imported surveys whose first page is a branded image."""
    v = rt.settings.get("show_title")
    if v is None:
        return os.environ.get("SURVEY_SHOW_TITLE", "true").strip().lower() in ("1", "true", "yes")
    return bool(v)


def render_page(survey_id, session, step, rt=None):
    rt = rt or RT
    page = step["page"]
    errors = step.get("errors", {})
    qs = "".join(render_question(q, errors) for q in page["questions"])
    theme = session_theme(session, rt)
    # AI disclosure on the first page when LLM analysis is enabled for this survey
    notice = ""
    if (page["page_id"] == rt.pages[0]["id"] and consent.uses_llm(rt.d)
            and os.environ.get("SURVEY_LLM_ENRICH", "").lower() in ("1", "true", "yes")):
        notice = f'<div class="q"><div class="title">🤖 AI notice</div><p class="meta">{escape(consent.ai_notice(rt.d))}</p></div>'
    # "Submit" on the last page (no eligible page follows given current answers), else "Continue →".
    # A repeated page with passes still to go is never the last page.
    it = page.get("iteration")
    more_passes = bool(it) and it["index"] < it["total"]
    nxt = rt._first_eligible_from(session.get("page_pointer", -1) + 1, session)
    btn = "Submit" if nxt is None and not more_passes else "Continue →"
    enc = ' enctype="multipart/form-data"' if any(q["type"] == "file" for q in page["questions"]) else ""
    title_h1 = f'<h1>{escape(rt.d["title"])}</h1>' if _show_title(rt) else ""
    # client fingerprint: capture once, on the first page (hidden field + a small inline script) —
    # only when the deployer has opted into network-metadata capture, and never in preview.
    fp_field = ('<input type=hidden name=__fp id=__fp>' + FINGERPRINT_JS
                if _capture_client_meta() and page["page_id"] == rt.pages[0]["id"]
                and not session.get("preview") else "")
    # page title (piped — on a repeated page it usually names the item: "About {{loop:label}}")
    page_title = f'<h2>{_md_inline(page.get("title_md") or page["title"])}</h2>' if page.get("title") else ""
    body = (notice + title_h1 + page_title
            + f'<form method=post action="/s/{survey_id}/submit"{enc}>'
            f'<input type=hidden name=session_id value="{escape(session["session_id"])}">'
            f'<input type=hidden name=page_id value="{escape(page["page_id"])}">'
            f'{fp_field}{qs}<button>{btn}</button></form>')
    return _html(body, rt.d["title"], theme=theme)


def render_terminal(session, terminal, rt=None):
    rt = rt or RT
    status = terminal["status"]
    head = {"completed": "✅ Complete", "disqualified": "🚫 Screened out",
            "overquota": "🪣 Over quota", "user_terminated": "↩︎ Closed"}.get(status, status)
    # The respondent-facing thank-you page: the message, then (if configured) the onward redirect as
    # the *final* action after it. No operator/debug surfaces here (passback status, raw URLs, hints).
    extra = ""
    redirect = terminal.get("redirect") or {}
    if status == "completed":
        url = redirect.get("task_url") or redirect.get("url")
        if redirect.get("kind") == "default" and not url:
            url = os.environ.get("SURVEY_DEFAULT_REDIRECT_URL")   # deployment-wide default redirect
        if url:
            extra = f'<p><a class="btn" href="{escape(url)}"><button>Continue →</button></a></p>'
    body = (f'<div class="term"><h1>{head}</h1>'
            f'<p>{escape(terminal.get("message", "Thanks — your responses were recorded."))}</p>{extra}</div>')
    # Opt-in LLM analysis values are stored on the response (derived + llm_audit) for the OPERATOR —
    # reports, exports, the console. They are never shown on this respondent-facing page.
    return _html(body, "Done", theme=session_theme(session, rt))


def terminal_destination(session, terminal):
    """The onward URL for a terminal: a survey's explicit spec redirect (per-survey override) wins, else
    the integration's configured per-status default (e.g. the `redirect` integration's panel URL with
    respondent/response ids). None -> no redirect, show the thank-you page."""
    r = terminal.get("redirect") or {}
    if r.get("kind") in ("url", "route"):
        return r.get("url") or r.get("task_url")          # already piped by the runtime
    dr = INTEGRATION.default_redirect(session, terminal.get("status", "completed"))
    return dr.get("url") if dr.get("kind") == "url" else None


def landing(theme=None):
    n = _demo_counter
    n["n"] += 1
    sid = RT.d["id"]
    fresh = n["n"]  # fresh rid each load so a persona can be re-run from scratch
    qtheme = f"&theme={theme}" if theme else ""   # carried into the survey you open
    # persona buttons — each respondent has their OWN behavior (real per-user gating)
    cards = []
    for uid, label, has in _PERSONAS:
        rid = f"{_RUN_TAG}-{fresh}"
        tag = "🔎 has streaming search" if has else "— no streaming search"
        cards.append(
            f'<div class="q"><div class="title">Respondent #{uid} · {tag}</div>'
            f'<p class="meta">{escape(label)}</p>'
            f'<a class="btn" href="/s/{sid}?uid={uid}&rid={rid}&group=demo&lang=en{qtheme}">'
            f'<button>Open as #{uid} →</button></a></div>')
    # theme picker restyles THIS page (and the chosen theme carries into the survey you then open)
    sw = " · ".join(
        (f'<b>{t}</b>' if t == (theme or RT.d.get("settings", {}).get("theme")) else f'<a href="/?theme={t}">{t}</a>')
        for t in themes.list_themes())
    body = (f'<h1>{escape(RT.d["title"])}</h1>'
            f'<div class="switcher">Theme (applies to this page <b>and</b> the survey you open): {sw}</div>'
            f'<p class="lead">An agentic survey engine you drive from your AI. Each respondent below has '
            f'their <b>own</b> behavioral history (the demo stands in for your data warehouse). Open one and watch '
            f'whether the “why did you search…?” page appears — it’s gated on whether <i>that</i> person '
            f'actually searched for a streaming service.</p>'
            f'<p class="lead">💬 <b><a href="/chat{qtheme}">Take the same survey as a conversation →</a></b> '
            f'(the identical JSON, delivered as a chat — with one adaptive AI follow-up).</p>'
            + "".join(cards) +
            f'<p class="meta">Then try the screen-outs (consent “No”, “Never used”, wrong attention answer). '
            f'Watch the receiver terminal for passbacks; storage = {os.environ.get("SURVEY_STORE") or "memory"}.</p>')
    return _html(body, "survey-agent demo", theme=theme)


def _brand():
    """Deployment brand mark for the service entry point — neutral default, overridable so a deployer
    can label the hosted runtime without forking. Shared with the operator console's SURVEY_CONSOLE_BRAND."""
    return os.environ.get("SURVEY_CONSOLE_BRAND", "survey-agent")


def multi_landing(theme=None):
    """Root of the hosted multi-survey service. Default: a neutral, self-contained *holding page* that
    does NOT enumerate the surveys on this service (listing them at a public root would leak the program
    to anyone who hits it) — respondents always arrive via a direct /s/<id> link. Styled to match the
    operator console, branded via SURVEY_CONSOLE_BRAND. Set SURVEY_LANDING_LIST=1 to instead list the
    live surveys (handy for a trusted/dev deployment); operators can always use `survey-agent list`."""
    brand = _brand()
    if os.environ.get("SURVEY_LANDING_LIST", "").strip().lower() in ("1", "true", "yes"):
        live = [e for e in defstore().list_surveys() if not e.get("missing_file")]
        items = []
        for e in live:
            sid = e["id"]
            title = e.get("title") or sid
            desc = e.get("description") or ""
            items.append(
                f'<li style="margin:.6em 0"><a href="/s/{escape(sid)}?uid=preview&rid=1&group=preview&lang=en">'
                f'<b>{escape(str(title))}</b></a> <code>{escape(sid)}</code>'
                + (f'<div class="desc">{escape(str(desc))}</div>' if desc else "") + "</li>")
        body = (f'<h1>Surveys</h1>'
                f'<p class="lead">{len(live)} survey(s) live on this service. Open one to take it, or share its '
                f'<code>/s/&lt;id&gt;</code> link. Add another by pushing a new file to <code>{escape(SURVEY_DIR)}/</code>.</p>'
                f'<ul style="list-style:none;padding:0">{"".join(items) or "<li>(no surveys found)</li>"}</ul>')
        return _html(body, "Surveys", theme=theme)
    # default: a neutral branded holding card (self-contained, console-styled — independent of the
    # survey themes so a service entry point doesn't pick up whatever palette a survey happens to use).
    tagline = os.environ.get("SURVEY_BRAND_TAGLINE", "An open survey engine.")
    b = escape(brand)
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{b}</title><style>"
        ":root{--bg:#0a0c10;--bg2:#0e1116;--line:#232b34;--ink:#e6edf3;--mut:#8a97a6;--dim:#5c6975;--acc:#46e0a0;"
        "--mono:ui-monospace,'SF Mono',SFMono-Regular,Menlo,Consolas,monospace;"
        "--sans:ui-sans-serif,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}"
        "*{box-sizing:border-box}html,body{margin:0;height:100%}"
        "body{background-color:var(--bg);"
        "background-image:radial-gradient(1200px 600px at 80% -10%,color-mix(in srgb,var(--acc) 9%,transparent) 0%,transparent 60%),"
        "radial-gradient(900px 500px at -10% 110%,color-mix(in srgb,var(--ink) 5%,transparent) 0%,transparent 55%);"
        "color:var(--ink);font:14px/1.55 var(--sans);-webkit-font-smoothing:antialiased;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}"
        ".card{width:100%;max-width:440px;background:linear-gradient(180deg,var(--bg2),var(--bg));"
        "border:1px solid var(--line);border-radius:16px;padding:40px 36px;box-shadow:0 30px 80px rgba(0,0,0,.45)}"
        ".brand{display:flex;align-items:center;gap:11px;font-family:var(--mono);font-weight:600;font-size:15px;letter-spacing:.04em}"
        ".brand .dot{width:10px;height:10px;border-radius:50%;background:var(--acc);box-shadow:0 0 12px var(--acc)}"
        "h1{font-family:var(--mono);font-size:30px;font-weight:600;letter-spacing:.01em;margin:26px 0 8px}"
        ".lead{color:var(--mut);margin:0 0 8px;font-size:15px}"
        ".foot{margin-top:26px;color:var(--dim);font:11px var(--mono);letter-spacing:.06em}"
        "</style></head><body><div class=card>"
        f"<div class=brand><span class=dot></span>{b}</div>"
        f"<h1>{b}</h1>"
        f"<p class=lead>{escape(tagline)}</p>"
        "<p class=lead>This is a private survey service. If you were invited to take part, please open "
        "the personal link you were given.</p>"
        "<div class=foot>private survey service</div>"
        "</div></body></html>")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, url, code=302):
        """302 the browser to `url` (the panel passback / onward navigation at a terminal)."""
        self.send_response(code)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _client_ip(self):
        return client_meta(self.headers, self.client_address).get("ip") or "-"

    def _rate_limited(self, retry):
        body = json.dumps({"error": "rate limited", "retry_after": retry}).encode()
        self.send_response(429)
        self.send_header("Retry-After", str(retry))
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, {k: v[0] for k, v in parse_qs(parsed.query).items()}
        # rate-limit only the survey-serving paths (page loads); never /health, /assets, /manage.
        if _RATE_LIMITER and (path.startswith("/s/") or path == "/chat"):
            ok, retry = _RATE_LIMITER.allow(self._client_ip())
            if not ok:
                return self._rate_limited(retry)
        if path in ("", "/"):
            return self._send(200, multi_landing(query.get("theme")) if MULTI else landing(query.get("theme")))
        if path == "/health":                       # load-balancer / PaaS health check
            code, payload = _health_check()
            return self._send(code, json.dumps(payload), ctype="application/json")
        if path == "/api/responses" or path.startswith("/api/responses/"):
            return self._api_responses(path, query)
        if path == "/chat":
            if MULTI:
                rt = load_rt(query.get("survey"))
                if rt is None:
                    return self._send(404, _html("<p>Open <code>/chat?survey=&lt;id&gt;</code> for a specific "
                                                 "survey, or pick one from <a href='/'>the index</a>.</p>"))
                st = None if _serve_all_states() else _survey_state(query.get("survey"))
                if st in ("paused", "closed"):
                    return self._send(200, _closed_page(st, theme=query.get("theme")))
                if st is not None and st not in _serve_states() and not _preview_ok(query):
                    return self._send(200, _not_open_page(theme=query.get("theme")))
                return self._send(200, chat_page(query.get("theme"), rt, query.get("survey")))
            return self._send(200, chat_page(query.get("theme")))
        if path == "/favicon.ico":
            return self._send(204, b"")
        if path.startswith("/assets/"):
            # serve survey-local static files (images/etc.) so a survey needn't depend on any
            # external CDN — self-contained and reliable behind ad-blockers/locked-down networks.
            import mimetypes
            rel = os.path.normpath(path[len("/assets/"):].lstrip("/"))
            fp = os.path.join(ASSETS_DIR, rel)
            if rel.startswith("..") or not os.path.isfile(fp):
                return self._send(404, _html("<p>Not found</p>"))
            with open(fp, "rb") as fh:
                return self._send(200, fh.read(),
                                  ctype=mimetypes.guess_type(fp)[0] or "application/octet-stream")
        if path == "/ledger":
            try:
                raw = urllib.request.urlopen(WEBHOOK_URL.rsplit("/", 1)[0] + "/ledger", timeout=3).read().decode()
            except Exception as e:
                raw = f"(panel unreachable: {e})"
            return self._send(200, _html(f"<h1>Panel ledger (the response-status store)</h1><pre>{escape(raw)}</pre>"
                                         f'<p><a href="/">← back</a></p>'))
        # management console (Tier 3) — opt-in (SURVEY_CONSOLE=1); NEVER on the public demo by default.
        # Reads the whole SURVEY_DIR (not just this runtime's one SURVEY_FILE) so it indexes the program.
        if path == "/manage" or (path.startswith("/manage/") and path.endswith("/report")):
            if not os.environ.get("SURVEY_CONSOLE"):
                return self._send(404, _html("<p>Not found — set SURVEY_CONSOLE=1 to enable the console.</p>"))
            from survey_agent import dashboard
            cat = defstore().list_surveys()
            if path == "/manage":
                links = {e["id"]: f"/manage/{e['id']}/report" for e in cat if not e.get("missing_file")}
                return self._send(200, dashboard.render_html(
                    cat, report_links=links, title="survey-agent — console",
                    subtitle=f"{len(cat)} survey(s) · live management console (reports from the store)"))
            sid = path[len("/manage/"):-len("/report")]
            ent = next((e for e in cat if e.get("id") == sid and not e.get("missing_file")), None)
            if not ent:
                return self._send(404, _html(f"<p>Unknown survey {escape(sid)}</p>"))
            from survey_agent import report as report_mod
            try:
                n = int(query.get("simulate") or 0)
            except ValueError:
                n = 0
            spec = defstore().get_definition(sid)
            if spec is None:
                return self._send(404, _html(f"<p>Unknown survey {escape(sid)}</p>"))
            rt = SurveyRuntime(spec)
            html = report_mod.generate(rt, store=(None if n else STORE), simulate_n=n, seed=7)
            return self._send(200, html)
        if path.startswith("/s/"):
            survey_id = path[3:].split("/")[0]
            theme = query.get("theme")
            rt = load_rt(survey_id) if MULTI else RT
            if rt is None:                          # multi mode: id not found in SURVEY_DIR
                return self._send(404, _html(
                    f"<div class='term'><h1>Unknown survey</h1><p>No survey <code>{escape(survey_id)}</code> "
                    f"on this service. Please check the link you were sent.</p></div>", theme=theme))
            # lifecycle gate (see _serve_states / _preview_ok); a local sandbox serves every state
            state = None if _serve_all_states() else _survey_state(survey_id)
            preview = _preview_ok(query)            # operator preview bypass (token-gated; collects nothing)
            if state in ("paused", "closed"):       # paused/closed: stop NEW respondents (in-flight may finish)
                u, r = rt.identity(query)
                live = STORE.get_session_by_id(f"{survey_id}:{r}:{u}")
                if not (live and live.get("status") not in TERMINAL_STATUSES):
                    return self._send(200, _closed_page(state, theme=theme))
            elif state is not None and state not in _serve_states() and not preview:
                # explicit pre-live state (draft/validated/tested/approved) — respondents can't reach it;
                # operators preview it. state None = unmanaged (single-file serve) → served as before.
                return self._send(200, _not_open_page(theme=theme))
            if not preview and not _signature_ok(query, rt):  # opt-in HMAC (forged link → no session); preview token suffices
                return self._send(403, _html(
                    "<div class='term'><h1>Link not valid</h1><p>This survey link is missing or has an "
                    "invalid signature. Please open the original link you were sent.</p></div>", theme=theme))
            uid, rid = rt.identity(query)          # configurable identity vars (settings.identity)
            session, created = STORE.get_or_create_session(
                survey_id, rid, uid,
                lambda: dict(rt.new_session(f"{survey_id}:{rid}:{uid}", query,
                                            response_id=_mint_response_id()),
                             date_started=now_iso(), **({"preview": True} if preview else {})))
            if theme:  # the theme chosen on the landing page sticks to the whole session
                session["theme"] = theme
                STORE.save_session(session)
            if session["status"] in TERMINAL_STATUSES:
                if rt.retake != "multiple" and not session.get("preview"):  # default "once": a finished link is locked
                    return self._send(200, _html(_html_inner_terminal_again(), theme=session_theme(session, rt)))
                # retake="multiple" (or a preview run): start a fresh take. The finished response was
                # already recorded (on_terminal), so we reuse the session doc (same resume key
                # survey:rid:uid) but mint a NEW unique response_id → a distinct response + passback.
                # In-progress sessions are NOT touched (they resume above).
                was_preview = session.get("preview")
                session = dict(rt.new_session(session["session_id"], query,
                                              response_id=_mint_response_id()), date_started=now_iso())
                if was_preview or preview:
                    session["preview"] = True
                if theme:
                    session["theme"] = theme
                STORE.save_session(session)
                created = True
            if created:
                if _capture_client_meta():
                    session["client"] = client_meta(self.headers, self.client_address)
                INTEGRATION.started(session)
                # prefetch-on-start: only for respondents who actually begin. In prod this is
                # an async warm against your warehouse/cache while the intro page renders; here static
                # fixtures resolve instantly. Values land on the session for piping + logic.
                enrich.prefetch_for_survey(rt.d, uid, ENRICH)
                session["enrichments"] = enrich.resolve_for_survey(rt.d, uid, ENRICH)
                STORE.save_session(session)
            step = rt.start(session, quota_state(rt)) if session["page_pointer"] < 0 else rt.current_page(session, quota_state(rt))
            STORE.save_session(session)
            if "terminal" in step:
                on_terminal(session, step["terminal"], rt)
                return self._send(200, render_terminal(session, step["terminal"], rt))
            return self._send(200, render_page(survey_id, session, step, rt))
        self._send(404, _html("<p>Not found</p>"))

    def do_POST(self):
        parsed = urlparse(self.path)
        if _RATE_LIMITER and (parsed.path.endswith("/submit") or parsed.path == "/chat/say"):
            ok, retry = _RATE_LIMITER.allow(self._client_ip())
            if not ok:
                return self._rate_limited(retry)
        if parsed.path == "/chat/say":                    # conversational (chat) surface
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except Exception:
                body = {}
            return self._send(200, json.dumps(_chat_reply(body)), ctype="application/json")
        if not parsed.path.startswith("/s/") or not parsed.path.endswith("/submit"):
            return self._send(404, _html("<p>Not found</p>"))
        survey_id = parsed.path[3:].split("/")[0]
        rt = load_rt(survey_id) if MULTI else RT
        if rt is None:
            log.warning("submit for unknown survey", extra={"survey_id": survey_id})
            return self._send(404, _html("<p>Unknown survey. Please check the link you were sent.</p>"))
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        form, files = parse_post(self.headers, body)
        session = STORE.get_session_by_id(form.get("session_id", [""])[0])
        if session is None:
            return self._send(400, _html("<p>Session not found — please reopen the original link you were sent.</p>"))
        if session.get("status") in TERMINAL_STATUSES:
            # Already finished (completed / screened-out / over-quota). Refuse to reprocess — this is
            # what stops a respondent hitting Back to the screen-out page and changing the answer to
            # get past it. No answers change, no passback re-fires.
            return self._send(200, _html(_html_inner_terminal_again(), theme=session_theme(session, rt)))
        page_id = form.get("page_id", [""])[0]
        if _capture_client_meta():                            # client JS fingerprint (first submit only)
            fp = _parse_fingerprint(form.get("__fp", [""])[0])
            if fp and not (session.get("client") or {}).get("fingerprint"):
                session.setdefault("client", {})["fingerprint"] = fp
        raw_answers, grid = {}, {}
        for key, values in form.items():
            if key in ("session_id", "page_id", "__fp"):
                continue
            if "::" in key:                                   # grid cell: "qid::rowid"
                qid, rid = key.split("::", 1)
                checkbox_grid = rt.questions_by_id.get(qid, {}).get("cell_type") == "checkbox"
                grid.setdefault(qid, {})[rid] = values if checkbox_grid else values[0]
                continue
            q = rt.questions_by_id.get(key, {})
            raw_answers[key] = values if q.get("type") in ("checkbox", "ranking", "dropdown_multi") else values[0]
        raw_answers.update(grid)                              # grid answers are {row_id: col_value} dicts
        for qid, flist in files.items():                     # `file` questions: store bytes, keep a reference
            if rt.questions_by_id.get(qid, {}).get("type") != "file":
                continue
            us = upload_store()
            refs = [us.put(survey_id, session.get("session_id"), qid, fn, data, content_type=ct)
                    for (fn, ct, data) in flist if fn and data]
            if refs:
                multi = (rt.questions_by_id.get(qid, {}).get("validation", {}).get("max_files") or 1) > 1
                raw_answers[qid] = refs if multi else refs[0]
        step = rt.submit(session, page_id, raw_answers, quota_state(rt))
        STORE.save_session(session)
        for hook in step.get("webhooks", []):    # deliver any webhook-action intents
            from survey_agent.integrations.webhook import deliver
            ok = _OUTBOX.deliver_or_enqueue(hook, deliver) if _OUTBOX else deliver(hook)
            if not ok:
                log.warning("webhook delivery failed%s", " — queued for retry" if _OUTBOX else "",
                            extra={"survey_id": survey_id, "url": hook.get("url")})
        if "terminal" in step:
            on_terminal(session, step["terminal"], rt)
            dest = terminal_destination(session, step["terminal"])
            if dest:                                          # browser-redirect passback (complete + screen-out)
                return self._redirect(dest)
            return self._send(200, render_terminal(session, step["terminal"], rt))
        return self._send(200, render_page(survey_id, session, step, rt))

    def _api_responses(self, path, query):
        """Token-gated data export: GET /api/responses[/<survey_id>][?format=csv]. The shared secret
        (SURVEY_API_TOKEN) is required via `Authorization: Bearer <t>` or `?token=`; unset ⇒ disabled."""
        if not API_TOKEN:
            return self._send(503, json.dumps({"result_ok": False,
                              "error": "data API disabled — set SURVEY_API_TOKEN to enable"}),
                              ctype="application/json")
        import hmac as _hmac
        auth = self.headers.get("Authorization", "")
        tok = auth[7:] if auth.startswith("Bearer ") else query.get("token", "")
        if not _hmac.compare_digest(tok or "", API_TOKEN):
            return self._send(401, json.dumps({"result_ok": False, "error": "unauthorized"}),
                              ctype="application/json")
        survey_id = path[len("/api/responses/"):].strip("/") or None if path.startswith("/api/responses/") else None
        rows = STORE.list_responses(survey_id=survey_id)
        if query.get("format") == "csv":
            return self._send(200, _responses_csv(rows), ctype="text/csv; charset=utf-8")
        return self._send(200, json.dumps({"result_ok": True, "survey_id": survey_id,
                                           "count": len(rows), "data": rows}), ctype="application/json")


def _html_inner_terminal_again():
    return ('<div class="term"><h1>Already done</h1>'
            "<p>You've already completed this — thanks again.</p></div>")


# ----------------------------------------------------------------- conversational (chat) surface
# The SAME survey, delivered as a chat in the browser — the showcase for conversational mode +
# adaptive probing. State is per-browser, in-process (fine for a demo / single instance).
_CHATS = {}


def _chat_reply(body):
    """Drive a per-browser Conversation. With no/unknown session_id, start fresh (resolving demo
    behavioral enrichment for a uid that has history, so the adaptive-probe page can appear) and
    return the opening; otherwise feed the reply and return the next message (or the terminal).
    In multi-survey mode the client passes `survey` (the id) so we drive the right survey."""
    import uuid
    from survey_agent.conversation import Conversation
    rt = (load_rt(body.get("survey")) if MULTI else RT) or RT
    sid = body.get("session_id")
    convo = _CHATS.get(sid)
    if convo is None:
        if rt is None:
            return {"session_id": None, "message": "No survey selected — open /chat?survey=<id>.", "done": True}
        try:
            enr = enrich.resolve_for_survey(rt.d, "1001", ENRICH)
        except Exception:
            enr = {}
        convo = Conversation(rt.d, url_vars={rt.respondent_var: "1001", rt.response_var: "1",
                                             "group": "demo", "lang": "en"}, enrichments=enr)
        sid = "c" + uuid.uuid4().hex[:10]
        _CHATS[sid] = convo
        out = convo.opening()
        return {"session_id": sid, "message": out["message"], "done": bool(out.get("done"))}
    out = convo.reply(body.get("reply", ""))
    resp = {"session_id": sid, "message": out["message"], "done": bool(out.get("done"))}
    if out.get("done"):
        resp["status"] = out.get("status")
        _CHATS.pop(sid, None)
    return resp


def chat_page(theme=None, rt=None, survey_id=None):
    rt = rt or RT
    _survey_js = json.dumps(survey_id or (rt.d["id"] if rt else None))
    body = (
        '<div class="q" style="max-width:640px;margin:0 auto">'
        f'<h1>{escape(rt.d["title"])} — as a conversation</h1>'
        '<p class="meta">The <b>same survey</b>, delivered as a chat. The engine still owns every '
        'branch, screen-out and quota; the AI only phrases each question, reads your free-text reply, '
        'and — on a thin open answer — may ask one follow-up. (Offline this uses a deterministic stub; '
        'add an API key for natural phrasing.)</p>'
        '<div id="log" style="margin:1em 0;min-height:180px"></div>'
        '<form id="f" onsubmit="return say()"><div style="display:flex;gap:8px">'
        '<input id="m" autocomplete=off placeholder="Type your answer…" style="flex:1" disabled>'
        '<button id="b" disabled>Send</button></div></form>'
        '<p class="meta"><a href="/">← back to the form demo</a></p></div>')
    js = """<script>
    var SID=null, SURVEY=__SURVEY__;
    function add(who,txt){var d=document.createElement('div');d.style.margin='8px 0';
      d.style.whiteSpace='pre-wrap';var s=document.createElement('b');s.textContent=who+': ';
      d.appendChild(s);d.appendChild(document.createTextNode(txt));
      document.getElementById('log').appendChild(d);window.scrollTo(0,document.body.scrollHeight);}
    function lock(on){var m=document.getElementById('m'),b=document.getElementById('b');
      m.disabled=on;b.disabled=on;if(!on)m.focus();}
    async function post(reply){
      var r=await fetch('/chat/say',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({session_id:SID,survey:SURVEY,reply:reply})});
      var j=await r.json();SID=j.session_id;add('Survey',j.message);
      if(j.done){lock(true);document.getElementById('m').placeholder='[ '+(j.status||'done')+' ]';}
      else lock(false);}
    function say(){var m=document.getElementById('m'),t=m.value.trim();if(!t)return false;
      add('You',t);m.value='';lock(true);post(t);return false;}
    post('');
    </script>""".replace("__SURVEY__", _survey_js)
    return _html(body + js, "Chat — survey-agent", theme)


def _responses_csv(rows):
    """Flatten export records to CSV: id,date_started,date_submitted,status + url.* + q.* columns
    (union across rows, so a mixed export still lines up). List/dict answers are JSON-encoded."""
    import csv
    import io
    base = ["id", "date_started", "date_submitted", "status"]
    uvars, dvars = [], []
    for r in rows:
        for k in (r.get("url_variables") or {}):
            if k not in uvars:
                uvars.append(k)
        for k in (r.get("survey_data") or {}):
            if k not in dvars:
                dvars.append(k)
    # network-metadata columns appear only when at least one row carries them (capture is opt-in),
    # so the CSV shape is unchanged for deployments that don't enable SURVEY_CAPTURE_CLIENT_META.
    has_client = any(r.get("client") for r in rows)
    cli = ["client.ip", "client.user_agent", "client.fp_hash"] if has_client else []
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(base + cli + [f"url.{k}" for k in uvars] + [f"q.{k}" for k in dvars])
    for r in rows:
        uv, dv = (r.get("url_variables") or {}), (r.get("survey_data") or {})
        row = [r.get(k) for k in base]
        if has_client:
            c = r.get("client") or {}
            row += [c.get("ip"), c.get("user_agent"), (c.get("fingerprint") or {}).get("hash")]
        row += [uv.get(k) for k in uvars]
        for k in dvars:
            v = dv.get(k)
            if isinstance(v, dict) and "answer" in v:   # export records wrap answers as {id,question,answer}
                v = v["answer"]
            row.append(json.dumps(v) if isinstance(v, (list, dict)) else v)
        w.writerow(row)
    return buf.getvalue()


def _run_preflight():
    """Print startup warnings and refuse to start on a fatal misconfiguration (unless the operator
    sets SURVEY_ALLOW_INSECURE=1). Surfaces dev-placeholder secrets, an unreachable store, and the
    ephemeral-disk data-loss trap before any respondent traffic arrives."""
    from survey_agent import preflight
    issues = preflight.runtime_preflight(store=STORE, sign_required=(not MULTI and SIGN_REQUIRED),
                                         url_secret=URL_SECRET)
    for lvl, msg in issues:
        if lvl != "fatal":
            print(f"[runtime] {'⚠️ ' if lvl == 'warn' else '✓'} {msg}")
    fatal = [m for lvl, m in issues if lvl == "fatal"]
    if fatal:
        for m in fatal:
            print(f"[runtime] ✖  {m}")
        if (os.environ.get("SURVEY_ALLOW_INSECURE") or "").lower() in ("1", "true", "yes"):
            print("[runtime] ⚠️  SURVEY_ALLOW_INSECURE is set — starting anyway despite the above.")
        else:
            raise SystemExit("[runtime] refusing to start — fix the issues above, or set "
                             "SURVEY_ALLOW_INSECURE=1 to override (not recommended for production).")


class PortInUse(SystemExit):
    """Raised (as a clean SystemExit, exit code 1) when the runtime can't bind its port."""


def bind_http(server_cls, handler, host, port, label="runtime"):
    """Bind an HTTP server or fail with ONE clear line (no traceback) — e.g. the port is in use."""
    try:
        return server_cls((host, port), handler)
    except OSError as e:
        import errno
        if e.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)):
            msg = (f"[{label}] ✖ port {port} on {host} is already in use — stop whatever is using it, "
                   f"or pick another with --port.")
        else:
            msg = f"[{label}] ✖ cannot listen on {host}:{port}: {e.strerror or e}"
        err = PortInUse(1)
        err.message = msg
        print(msg, file=sys.stderr)
        raise err


def bind(port=8000, host=None):
    """Resolve host/port, run the startup preflight, and bind the socket. Returns the server (not yet
    serving) so a caller (e.g. `demo`) can bind in its main thread and fail cleanly before starting."""
    # bind/port are env-aware so the same command hosts a public demo on any PaaS: SURVEY_HOST=0.0.0.0
    # and $PORT (a PaaS injects it). Defaults stay local-only.
    host = host or os.environ.get("SURVEY_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT") or port)
    _run_preflight()
    httpd = bind_http(ThreadingHTTPServer, Handler, host, port)
    httpd._sa_host, httpd._sa_port = host, port
    return httpd


def serve(port=8000, host=None, httpd=None):
    httpd = httpd or bind(port, host)
    host, port = httpd._sa_host, httpd._sa_port

    # Graceful shutdown: on SIGTERM/SIGINT (a PaaS sends SIGTERM on redeploy) flip the drain flag so
    # /health 503s, then stop accepting new connections from a side thread (shutdown() must not run
    # in the serve_forever() thread). In-flight request threads finish; we close the socket after.
    def _drain(signum, _frame):
        global _SHUTTING_DOWN
        _SHUTTING_DOWN = True
        print(f"[runtime] received signal {signum} — draining, no new connections accepted.")
        threading.Thread(target=httpd.shutdown, daemon=True).start()
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _drain)
        except (ValueError, OSError):  # pragma: no cover — not in the main thread (e.g. under a test)
            pass

    # Background outbox sweep: retry queued webhook deliveries every N seconds (opt-in).
    if _OUTBOX and (os.environ.get("SURVEY_OUTBOX_RETRY_SECONDS") or "").strip():
        secs = max(5, int(os.environ["SURVEY_OUTBOX_RETRY_SECONDS"]))
        from survey_agent.integrations.webhook import deliver as _wdeliver

        def _sweep():
            while not _SHUTTING_DOWN:
                time.sleep(secs)
                try:
                    r = _OUTBOX.retry(_wdeliver)
                    if r["attempted"]:
                        log.info("outbox sweep", extra=r)
                except Exception:
                    log.exception("outbox sweep failed")
        threading.Thread(target=_sweep, daemon=True).start()
        print(f"[runtime] ✓ webhook outbox retry sweep every {secs}s (pending={_OUTBOX.stats()['pending']})")

    if MULTI:
        n = len([e for e in defstore().list_surveys() if not e.get("missing_file")])
        print(f"[runtime] multi-survey runtime on http://{host}:{port}  · dir='{SURVEY_DIR}' ({n} survey(s)) "
              f"· store={os.environ.get('SURVEY_STORE') or 'memory'} · data API={'on' if API_TOKEN else 'off'} "
              f"· index: /  form: /s/<id>  chat: /chat?survey=<id>")
    else:
        print(f"[runtime] survey runtime on http://{host}:{port}  · survey='{RT.d['id']}' "
              f"theme='{RT.d.get('settings',{}).get('theme') or _default_theme()}' · store={os.environ.get('SURVEY_STORE') or 'memory'} "
              f"· integration={_INT_KIND} · form: /  chat: /chat"
              + (f" · DEMO_QUOTA_LIMIT={DEMO_QUOTA_LIMIT}" if DEMO_QUOTA_LIMIT else ""))
    if _serve_all_states():
        print("[runtime] local preview: serving EVERY lifecycle state (drafts included) — not for respondents.")
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


if __name__ == "__main__":
    serve()
