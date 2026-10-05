"""End-to-end HTTP tests for the demo runtime (server.py) — the surface a real respondent actually
touches. Covers GET render, a POST that advances to a terminal, HTML-escaping of reflected URL vars
(XSS), and enforcement of opt-in URL-param signing. Each test reloads server.py against a hermetic
temp survey + controlled env, so nothing depends on the bundled example or a running receiver."""
import contextlib
import importlib
import json
import os
import re
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from survey_agent.engine import signing


def _survey(extra_settings=None):
    settings = {"url_variables": ["uid", "rid", "group"], "theme": "base"}
    settings.update(extra_settings or {})
    return {"survey": {
        "id": "t_http", "version": 1, "title": "HTTP test", "settings": settings,
        "pages": [{"id": "p1", "questions": [
            {"id": "q_consent", "type": "radio", "required": True, "title": "Do you consent?",
             "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}]}]}],
        "actions": [{"type": "complete", "redirect": {"kind": "default"}}],
    }}


@contextlib.contextmanager
def running_server(spec, **env):
    fd, path = tempfile.mkstemp(suffix=".survey.json")
    with os.fdopen(fd, "w") as f:
        json.dump(spec, f)
    old = dict(os.environ)
    os.environ.update({"SURVEY_FILE": path, "SURVEY_STORE": "memory", "SURVEY_INTEGRATION": "none"})
    os.environ.update({k: str(v) for k, v in env.items()})
    import survey_agent.server as server
    importlib.reload(server)                       # rebuild module state (SPEC/RT/STORE) from this env
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        os.environ.clear()
        os.environ.update(old)
        os.remove(path)


def _get(url):
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _post(url, data):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    with urllib.request.urlopen(req) as r:
        return r.status, r.read().decode()


def test_get_renders_first_page():
    with running_server(_survey()) as base:
        status, html = _get(f"{base}/s/t_http?uid=1&rid=2&group=demo&lang=en")
        assert status == 200
        assert "Do you consent?" in html
        assert 'name="q_consent"' in html


def test_reflected_url_var_is_html_escaped():
    with running_server(_survey()) as base:
        q = urllib.parse.urlencode({"uid": "<script>alert(1)</script>", "rid": "2", "group": "demo"})
        status, html = _get(f"{base}/s/t_http?{q}")
        assert status == 200
        assert "<script>alert(1)</script>" not in html       # not reflected raw
        assert "&lt;script&gt;" in html                       # escaped instead


def test_post_advances_to_terminal():
    with running_server(_survey()) as base:
        _, html = _get(f"{base}/s/t_http?uid=10&rid=20&group=demo")
        sid = re.search(r'name=session_id value="([^"]+)"', html).group(1)
        pid = re.search(r'name=page_id value="([^"]+)"', html).group(1)
        status, done = _post(f"{base}/s/t_http/submit",
                             {"session_id": sid, "page_id": pid, "q_consent": "yes"})
        assert status == 200
        assert "Complete" in done                             # render_terminal header for a completion


def test_url_signing_rejects_unsigned_and_accepts_signed():
    secret = "s3cret"
    spec = _survey({"url_signing": {"required": True, "fields": ["uid", "rid", "group"]}})
    with running_server(spec, SURVEY_URL_SECRET=secret) as base:
        # unsigned (or forged) link → no session is created
        status, html = _get(f"{base}/s/t_http?uid=1&rid=2&group=vip")
        assert status == 403
        assert "Link not valid" in html
        # a correctly signed link is admitted
        q = signing.signed_query({"uid": "1", "rid": "2", "group": "vip"}, secret, ["uid", "rid", "group"])
        status, html = _get(f"{base}/s/t_http?{q}")
        assert status == 200
        assert "Do you consent?" in html


def test_url_signing_not_enforced_without_a_secret():
    # required=true but no SURVEY_URL_SECRET configured → fail OPEN (with a startup warning), so a
    # misconfiguration doesn't silently lock everyone out; the link still works.
    spec = _survey({"url_signing": {"required": True, "fields": ["uid", "rid", "group"]}})
    with running_server(spec) as base:
        status, _ = _get(f"{base}/s/t_http?uid=1&rid=2&group=vip")
        assert status == 200


def _two_page_survey():
    return {"survey": {
        "id": "t_http2", "version": 1, "title": "Two", "settings": {"url_variables": ["uid", "rid"], "theme": "base"},
        "pages": [
            {"id": "p1", "questions": [{"id": "q1", "type": "radio", "required": True, "title": "Q1?",
                                        "options": [{"value": "a", "label": "A"}]}]},
            {"id": "p2", "questions": [{"id": "q2", "type": "radio", "required": True, "title": "Q2?",
                                        "options": [{"value": "b", "label": "B"}]}]}],
        "actions": [{"type": "complete", "redirect": {"kind": "default"}}],
    }}


def test_last_page_button_says_submit_others_continue():
    with running_server(_two_page_survey()) as base:
        _, h1 = _get(f"{base}/s/t_http2?uid=1&rid=2")
        assert "<button>Continue →</button>" in h1            # an intermediate page advances
        sid = re.search(r'name=session_id value="([^"]+)"', h1).group(1)
        pid = re.search(r'name=page_id value="([^"]+)"', h1).group(1)
        _, h2 = _post(f"{base}/s/t_http2/submit", {"session_id": sid, "page_id": pid, "q1": "a"})
        assert "<button>Submit</button>" in h2                # the last page submits


def test_terminal_is_a_clean_thank_you():
    # the respondent-facing terminal carries no demo links ("start again") or operator/debug telemetry
    with running_server(_survey()) as base:
        _, h = _get(f"{base}/s/t_http?uid=10&rid=20&group=demo")
        sid = re.search(r'name=session_id value="([^"]+)"', h).group(1)
        pid = re.search(r'name=page_id value="([^"]+)"', h).group(1)
        _, done = _post(f"{base}/s/t_http/submit", {"session_id": sid, "page_id": pid, "q_consent": "yes"})
        assert "Complete" in done
        assert "start again" not in done
        assert "passback was sent" not in done


@contextlib.contextmanager
def running_multi(specs, state=None, **env):
    """A multi-survey runtime over a temp SURVEY_DIR; `state` maps survey_id → lifecycle state.
    Yields (base_url, dir) so a test can also flip state at runtime via registry.set_status."""
    import shutil
    import survey_agent.registry as registry
    d = tempfile.mkdtemp()
    for spec in specs:
        with open(os.path.join(d, f"{spec['survey']['id']}.survey.json"), "w") as f:
            json.dump(spec, f)
    for sid, st in (state or {}).items():
        registry.set_status(sid, status=st, sdir=d)
    old = dict(os.environ)
    os.environ.pop("SURVEY_FILE", None)
    os.environ.update({"SURVEY_DIR": d, "SURVEY_MULTI": "1", "SURVEY_STORE": "memory", "SURVEY_INTEGRATION": "none"})
    os.environ.update({k: str(v) for k, v in env.items()})
    import survey_agent.server as server
    importlib.reload(server)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", d
    finally:
        httpd.shutdown()
        os.environ.clear()
        os.environ.update(old)
        shutil.rmtree(d, ignore_errors=True)


def test_paused_survey_shows_closed_page_to_new_respondents():
    with running_multi([_survey()], state={"t_http": "paused"}) as (base, _d):
        _, html = _get(f"{base}/s/t_http?uid=1&rid=2")
        assert "Paused" in html and "Do you consent?" not in html


def test_closed_survey_shows_closed_page():
    with running_multi([_survey()], state={"t_http": "closed"}) as (base, _d):
        _, html = _get(f"{base}/s/t_http?uid=1&rid=2")
        assert "Closed" in html and "Do you consent?" not in html


def test_live_survey_serves_in_multi_mode():
    with running_multi([_survey()], state={"t_http": "live"}) as (base, _d):
        _, html = _get(f"{base}/s/t_http?uid=1&rid=2")
        assert "Do you consent?" in html


def test_pause_lets_in_flight_resume_but_blocks_new():
    import survey_agent.registry as registry
    with running_multi([_survey()], state={"t_http": "live"}) as (base, d):  # live → collecting
        _, started = _get(f"{base}/s/t_http?uid=9&rid=8")      # one respondent begins
        assert "Do you consent?" in started
        registry.set_status("t_http", status="paused", sdir=d)  # pause mid-field
        _, resume = _get(f"{base}/s/t_http?uid=9&rid=8")        # same link → in-flight resumes
        assert "Do you consent?" in resume
        _, fresh = _get(f"{base}/s/t_http?uid=1&rid=2")         # a new link → closed
        assert "Paused" in fresh and "Do you consent?" not in fresh


def test_draft_is_not_open_to_respondents():
    # the gate: a survey with an explicit pre-live state (draft) does NOT serve real respondents
    with running_multi([_survey()], state={"t_http": "draft"}) as (base, _d):
        _, html = _get(f"{base}/s/t_http?uid=1&rid=2")
        assert "Not open yet" in html and "Do you consent?" not in html


def test_preview_token_bypasses_the_gate_but_collects_nothing():
    import survey_agent.server as server
    with running_multi([_survey()], state={"t_http": "draft"},
                       SURVEY_PREVIEW_TOKEN="seekrit") as (base, _d):
        # no token → gated
        _, blocked = _get(f"{base}/s/t_http?uid=1&rid=2")
        assert "Not open yet" in blocked
        # wrong token → still gated
        _, bad = _get(f"{base}/s/t_http?uid=1&rid=2&preview=nope")
        assert "Not open yet" in bad
        # right token → renders the survey, and runs through to a terminal recording nothing
        _, ok = _get(f"{base}/s/t_http?uid=prev&rid=1&preview=seekrit")
        assert "Do you consent?" in ok
        sid = re.search(r'name=session_id value="([^"]+)"', ok).group(1)
        pid = re.search(r'name=page_id value="([^"]+)"', ok).group(1)
        status, done = _post(f"{base}/s/t_http/submit",
                             {"session_id": sid, "page_id": pid, "q_consent": "yes"})
        assert status == 200 and "Complete" in done             # preview reaches the terminal page
        assert server.STORE.list_responses("t_http") == []      # …but collected no data


def test_serve_states_override_widens_the_gate():
    # rollout lever: operators can temporarily serve pre-live states during a migration
    with running_multi([_survey()], state={"t_http": "tested"},
                       SURVEY_SERVE_STATES="live,tested") as (base, _d):
        _, html = _get(f"{base}/s/t_http?uid=1&rid=2")
        assert "Do you consent?" in html


# --- settings.show_title / SURVEY_SHOW_TITLE: the per-page survey-title <h1> banner ---

def test_title_banner_shown_by_default():
    with running_server(_survey()) as base:
        _, html = _get(f"{base}/s/t_http?uid=1&rid=2&group=demo")
        assert "<h1>HTTP test</h1>" in html

def test_show_title_false_hides_the_banner():
    with running_server(_survey({"show_title": False})) as base:
        _, html = _get(f"{base}/s/t_http?uid=1&rid=2&group=demo")
        assert "<h1>HTTP test</h1>" not in html
        assert "Do you consent?" in html                      # questions still render

def test_deployment_default_can_hide_title_via_env():
    # SURVEY_SHOW_TITLE=false flips the whole-deployment default (a survey can still opt back in)
    with running_server(_survey(), SURVEY_SHOW_TITLE="false") as base:
        _, html = _get(f"{base}/s/t_http?uid=1&rid=2&group=demo")
        assert "<h1>HTTP test</h1>" not in html
    with running_server(_survey({"show_title": True}), SURVEY_SHOW_TITLE="false") as base:
        _, html = _get(f"{base}/s/t_http?uid=1&rid=2&group=demo")
        assert "<h1>HTTP test</h1>" in html                   # per-survey setting overrides the env


# --- network metadata capture (SURVEY_CAPTURE_CLIENT_META) — opt-in, default off ---

def test_client_meta_not_captured_by_default():
    # the open core does NOT capture IP / fingerprint unless the deployer opts in
    with running_server(_survey()) as base:
        _, html = _get(f"{base}/s/t_http?uid=1&rid=2&group=demo")
        assert "name=__fp" not in html and "FINGERPRINT" not in html

def test_client_meta_opt_in_injects_fingerprint_field():
    with running_server(_survey(), SURVEY_CAPTURE_CLIENT_META="1") as base:
        _, html = _get(f"{base}/s/t_http?uid=1&rid=2&group=demo")
        assert "name=__fp" in html                            # the hidden fingerprint field + script

def test_client_meta_uses_leftmost_xff():
    # behind a proxy the real client IP is the left-most X-Forwarded-For entry, not the socket peer
    from survey_agent.server import client_meta
    meta = client_meta({"X-Forwarded-For": "203.0.113.7, 10.0.0.1", "User-Agent": "UA/1"}, ("10.0.0.1", 5))
    assert meta["ip"] == "203.0.113.7" and meta["ip_chain"] == ["203.0.113.7", "10.0.0.1"]
    assert client_meta({}, ("198.51.100.9", 5))["ip"] == "198.51.100.9"   # falls back to the peer

def test_export_carries_client_only_when_present():
    from survey_agent.engine import export
    from survey_agent.engine.runtime import SurveyRuntime
    rt = SurveyRuntime(_survey())
    s = rt.new_session("t_http:rid:uid", {"uid": "1", "rid": "2", "group": "demo"})
    assert "client" not in export.response_from_session(rt, s)         # absent unless captured
    s["client"] = {"ip": "203.0.113.7", "fingerprint": {"hash": "abc123"}}
    assert export.response_from_session(rt, s)["client"]["ip"] == "203.0.113.7"


# --- multi-survey landing page: neutral holding card by default, opt-in listing, brandable ---

def test_landing_is_a_holding_page_by_default():
    # the bare root must NOT enumerate surveys (program leak); shows only branding
    with running_multi([_survey()], state={"t_http": "live"}) as (base, _d):
        _, html = _get(f"{base}/")
        assert "survey-agent" in html                          # neutral default brand mark
        assert "survey(s) live on this service" not in html and "t_http" not in html

def test_landing_lists_surveys_when_opted_in():
    with running_multi([_survey()], state={"t_http": "live"}, SURVEY_LANDING_LIST="1") as (base, _d):
        _, html = _get(f"{base}/")
        assert "survey(s) live on this service" in html and "t_http" in html

def test_landing_brand_is_overridable():
    with running_multi([_survey()], state={"t_http": "live"}, SURVEY_CONSOLE_BRAND="acme-research") as (base, _d):
        _, html = _get(f"{base}/")
        assert "acme-research" in html
