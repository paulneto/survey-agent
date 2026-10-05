"""panel — server-side lifecycle passback to a survey panel / sample provider.

Maps each of the four neutral lifecycle events to its own configured URL and delivers the neutral
event payload there, signed with a shared secret. It is **generic**: the URLs and the secret are
config, so no panel/vendor name lives in this code (the `engine` stays vendor-free; cf. `webhook`,
which posts every event to a single URL, and `mailchimp`, a worked CRM example).

    SURVEY_INTEGRATION=panel
    SURVEY_PANEL_STARTED_URL                   # event=started (once, when a respondent begins)
    SURVEY_PANEL_COMPLETED_URL                 # event=completed
    SURVEY_PANEL_TERMINATED_URL                # event=terminated (disqualified | overquota)
    SURVEY_PANEL_TERMINATED_BY_USER_URL        # event=terminated_by_user
    SURVEY_PANEL_SHARED_SECRET                 # HMAC signs the call (same scheme as `webhook`)
    SURVEY_PANEL_CALLBACK_METHOD=POST          # POST (signed JSON, default) | GET (event as query string)

The payload is the neutral event from `engine.terminal.build_event` — it carries `url_variables`
(uid/rid/utm/… whatever the survey_url passed) and a stable `idempotency_key` so the panel can
dedupe under at-least-once retry. A panel with a bespoke wire format can still ship its own plugin
and select it by name; this built-in covers the common "POST the event to a per-status URL" case.
"""
import os

from survey_agent.engine import terminal
from survey_agent.engine.runtime import (
    EVENT_STARTED, EVENT_COMPLETED, EVENT_TERMINATED, EVENT_TERMINATED_BY_USER,
)
from .base import Integration
from .webhook import deliver  # reuse the signed, best-effort, at-least-once delivery

# lifecycle event name -> the env var holding its callback URL
URL_ENV = {
    EVENT_STARTED: "SURVEY_PANEL_STARTED_URL",
    EVENT_COMPLETED: "SURVEY_PANEL_COMPLETED_URL",
    EVENT_TERMINATED: "SURVEY_PANEL_TERMINATED_URL",
    EVENT_TERMINATED_BY_USER: "SURVEY_PANEL_TERMINATED_BY_USER_URL",
}


class PanelIntegration(Integration):
    def __init__(self, urls=None, secret=None, method=None):
        self.urls = urls if urls is not None else {ev: os.environ.get(env) for ev, env in URL_ENV.items()}
        self.secret = secret if secret is not None else os.environ.get("SURVEY_PANEL_SHARED_SECRET", "")
        self.method = (method or os.environ.get("SURVEY_PANEL_CALLBACK_METHOD", "POST")).upper()

    def started(self, session):
        self._deliver(terminal.build_event(session, "started"))

    def terminal(self, session, status, reason_code=None):
        self._deliver(terminal.build_event(session, status, reason_code=reason_code))

    def _deliver(self, event):
        url = (self.urls or {}).get(event["event"])
        if not url:
            return  # this event isn't subscribed -> no-op (best-effort, never raises)
        deliver({"method": self.method, "url": url, "payload": event}, secret=self.secret)
