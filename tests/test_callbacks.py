"""The generic terminal-event mapping (engine.terminal) + the built-in integrations. The `panel`
built-in is generic (per-status URLs are config); a panel with a bespoke wire format still ships as
its own (e.g. private) plugin and is tested there."""
from survey_agent.engine import terminal
from survey_agent.engine.runtime import (
    STATUS_COMPLETED, STATUS_DISQUALIFIED, STATUS_OVERQUOTA, STATUS_USER_TERMINATED,
    EVENT_COMPLETED, EVENT_TERMINATED, EVENT_TERMINATED_BY_USER,
)
import survey_agent.integrations as integrations
from survey_agent.integrations.base import Integration
from survey_agent.integrations.redirect import RedirectIntegration
from survey_agent.integrations.webhook import WebhookIntegration
from survey_agent.integrations.panel import PanelIntegration


SESSION = {
    "session_id": "screener:r-2001:u-1001",
    "survey_id": "screener",
    "url_vars": {"uid": "u-1001", "rid": "r-2001", "group": "demo"},
}


def test_status_to_event_mapping():
    assert terminal.STATUS_TO_EVENT[STATUS_COMPLETED] == EVENT_COMPLETED
    assert terminal.STATUS_TO_EVENT[STATUS_DISQUALIFIED] == EVENT_TERMINATED
    assert terminal.STATUS_TO_EVENT[STATUS_OVERQUOTA] == EVENT_TERMINATED
    assert terminal.STATUS_TO_EVENT[STATUS_USER_TERMINATED] == EVENT_TERMINATED_BY_USER


def test_idempotency_key_stable_and_specific():
    a = terminal.idempotency_key("resp-1", "completed")
    assert a == terminal.idempotency_key("resp-1", "completed")
    assert a != terminal.idempotency_key("resp-1", "disqualified")
    assert a != terminal.idempotency_key("resp-2", "completed")


def test_build_event_is_neutral_but_carries_url_vars():
    e = terminal.build_event(SESSION, STATUS_COMPLETED)
    assert e["event"] == "completed" and e["status"] == "completed"
    assert e["response_id"] == SESSION["session_id"]
    assert e["url_variables"] == SESSION["url_vars"]   # uid/rid travel here
    assert "uid" not in e                              # the engine hardcodes no panel fields


def test_get_integration_builtins():
    assert isinstance(integrations.get_integration("none"), Integration)
    assert isinstance(integrations.get_integration("redirect"), RedirectIntegration)
    assert isinstance(integrations.get_integration("webhook"), WebhookIntegration)
    assert isinstance(integrations.get_integration("panel"), PanelIntegration)


def test_unknown_integration_raises():
    import pytest
    with pytest.raises(ValueError):
        integrations.get_integration("nope")


def test_webhook_is_safe_noop_without_url():
    # no URL configured -> started/terminal must not raise (delivery is best-effort)
    wh = WebhookIntegration(url=None)
    wh.started(SESSION)
    wh.terminal(SESSION, "completed")


def test_base_default_redirect_is_noop():
    assert Integration().default_redirect(SESSION) == {"kind": "default"}
    assert Integration().default_redirect(SESSION, "disqualified") == {"kind": "default"}


# ---- redirect integration: per-status URL + pid/tid passback ----

REDIRECT_URLS = {"completed": "https://panel.example/complete", "disqualified": "https://panel.example/terminate"}


def test_redirect_per_status_with_passback():
    r = RedirectIntegration(urls=REDIRECT_URLS, passback=["rid", "uid"])
    # SESSION.url_vars = {"uid":"u-1001","rid":"r-2001","group":"demo"} — only rid/uid passed back, in order
    assert r.default_redirect(SESSION, "completed") == {"kind": "url", "url": "https://panel.example/complete?rid=r-2001&uid=u-1001"}
    assert r.default_redirect(SESSION, "disqualified") == {"kind": "url", "url": "https://panel.example/terminate?rid=r-2001&uid=u-1001"}


def test_redirect_overquota_falls_back_to_terminated(monkeypatch):
    monkeypatch.setenv("SURVEY_REDIRECT_TERMINATED_URL", "https://s/term")
    monkeypatch.delenv("SURVEY_REDIRECT_OVERQUOTA_URL", raising=False)
    monkeypatch.delenv("SURVEY_REDIRECT_COMPLETE_URL", raising=False)
    r = RedirectIntegration(passback=[])
    assert r.default_redirect(SESSION, "overquota") == {"kind": "url", "url": "https://s/term"}


def test_redirect_noop_when_unconfigured():
    r = RedirectIntegration(urls={}, passback=[])
    assert r.default_redirect(SESSION, "completed") == {"kind": "default"}


def test_redirect_appends_to_existing_query():
    r = RedirectIntegration(urls={"completed": "https://s/c?x=1"}, passback=["uid"])
    assert r.default_redirect(SESSION, "completed")["url"] == "https://s/c?x=1&uid=u-1001"


# ---- runtime carries a redirect on every terminal (not just complete) ----

def test_runtime_attaches_redirect_to_all_terminals():
    from survey_agent.engine.runtime import SurveyRuntime
    survey = {"survey": {
        "id": "r", "title": "R", "settings": {"url_variables": ["pid", "tid"]},
        "pages": [{"id": "p1", "questions": [
            {"id": "q1", "type": "radio", "required": True,
             "options": [{"value": "yes", "label": "Yes"}, {"value": "no", "label": "No"}]}]}],
        "actions": [
            {"type": "disqualify", "when": {"ref": {"kind": "question", "id": "q1"}, "operator": "equals", "value": "no"},
             "redirect": {"kind": "url", "url": "https://panelB/term?pid={{url:pid}}"}},   # per-survey override, piped
            {"type": "complete", "redirect": {"kind": "default"}},
        ],
    }}
    rt = SurveyRuntime(survey)
    qs = rt.initial_quota_state()
    # screen-out path carries the (piped) override redirect
    s = rt.new_session("r:1:1", {"pid": "C1", "tid": "S1"})
    rt.start(s, qs)
    dq = rt.submit(s, "p1", {"q1": "no"}, qs)["terminal"]
    assert dq["status"] == "disqualified"
    assert dq["redirect"] == {"kind": "url", "url": "https://panelB/term?pid=C1"}
    # complete path carries kind=default (server maps it to the configured URL)
    s2 = rt.new_session("r:1:2", {"pid": "C2", "tid": "S2"})
    rt.start(s2, qs)
    done = rt.submit(s2, "p1", {"q1": "yes"}, qs)["terminal"]
    assert done["status"] == "completed" and done["redirect"] == {"kind": "default"}


# ---- panel: per-status passback ----

PANEL_URLS = {
    EVENT_COMPLETED: "https://panel.example/completed",
    EVENT_TERMINATED: "https://panel.example/terminated",
    EVENT_TERMINATED_BY_USER: "https://panel.example/terminated-by-user",
    "started": "https://panel.example/started",
}


def _capturing_panel(monkeypatch, **kw):
    """A PanelIntegration whose delivery is captured instead of sent over the network."""
    sent = []
    import survey_agent.integrations.panel as panel_mod
    monkeypatch.setattr(panel_mod, "deliver", lambda intent, secret=None: sent.append((intent, secret)) or True)
    return PanelIntegration(urls=PANEL_URLS, secret="shh", **kw), sent


def test_panel_routes_each_status_to_its_own_url(monkeypatch):
    p, sent = _capturing_panel(monkeypatch)
    p.started(SESSION)
    p.terminal(SESSION, STATUS_COMPLETED)
    p.terminal(SESSION, STATUS_DISQUALIFIED)          # -> "terminated"
    p.terminal(SESSION, STATUS_OVERQUOTA)             # -> "terminated" too
    p.terminal(SESSION, STATUS_USER_TERMINATED)       # -> "terminated_by_user"
    urls = [i["url"] for i, _ in sent]
    assert urls == [
        "https://panel.example/started",
        "https://panel.example/completed",
        "https://panel.example/terminated",
        "https://panel.example/terminated",
        "https://panel.example/terminated-by-user",
    ]


def test_panel_passes_neutral_event_and_secret(monkeypatch):
    p, sent = _capturing_panel(monkeypatch)
    p.terminal(SESSION, STATUS_COMPLETED, reason_code="ok")
    intent, secret = sent[0]
    assert secret == "shh"
    assert intent["method"] == "POST"
    ev = intent["payload"]
    assert ev["status"] == "completed" and ev["reason_code"] == "ok"
    assert ev["url_variables"] == SESSION["url_vars"]   # tid/pid/… ride along
    assert ev["idempotency_key"]                        # dedupe under retry


def test_panel_unsubscribed_event_is_noop(monkeypatch):
    p, sent = _capturing_panel(monkeypatch)
    p.urls = {EVENT_COMPLETED: "https://panel.example/completed"}  # only completed configured
    p.started(SESSION)                 # no started URL -> no-op
    p.terminal(SESSION, STATUS_DISQUALIFIED)  # no terminated URL -> no-op
    p.terminal(SESSION, STATUS_COMPLETED)
    assert [i["url"] for i, _ in sent] == ["https://panel.example/completed"]


def test_panel_safe_without_any_config():
    # SURVEY_INTEGRATION=panel but no URLs set -> started/terminal must not raise
    p = PanelIntegration(urls={}, secret="")
    p.started(SESSION)
    p.terminal(SESSION, "completed")
