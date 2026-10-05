"""redirect — browser-redirect passback. On a terminal, send the respondent to a per-status URL with
selected url_variables appended (e.g. uid/rid) — the classic panel pattern: bounce a completer to the
panel's complete URL and a screen-out to its terminate URL. No server-side POST; the browser
navigation *is* the passback. This is the deployment **default**; a survey overrides any terminal with
a spec redirect of kind `url` (e.g. to point at a different panel).

Config:
  SURVEY_REDIRECT_COMPLETE_URL       completed
  SURVEY_REDIRECT_TERMINATED_URL     disqualified / overquota / user_terminated (the screen-out URL)
  SURVEY_REDIRECT_OVERQUOTA_URL      optional; overquota only, else falls back to the terminated URL
  SURVEY_REDIRECT_URL / SURVEY_DEFAULT_REDIRECT_URL   generic fallback for any terminal
  SURVEY_REDIRECT_PASSBACK=uid,rid   url_variables to append as query params (default: none)
"""
import os
from urllib.parse import urlencode

from .base import Integration

# terminal status -> primary env var holding its redirect URL
_ENV = {
    "completed": "SURVEY_REDIRECT_COMPLETE_URL",
    "disqualified": "SURVEY_REDIRECT_TERMINATED_URL",
    "overquota": "SURVEY_REDIRECT_OVERQUOTA_URL",
    "user_terminated": "SURVEY_REDIRECT_TERMINATED_URL",
}


class RedirectIntegration(Integration):
    def __init__(self, urls=None, passback=None, url=None):
        # urls/passback injectable for tests; otherwise read from the environment. `url` is a legacy
        # single-URL fallback (used for every terminal when no per-status URL is set).
        self.urls = urls
        self.fallback = url
        self.passback = (passback if passback is not None
                         else [v.strip() for v in os.environ.get("SURVEY_REDIRECT_PASSBACK", "").split(",") if v.strip()])

    def _base(self, status):
        if self.urls is not None:                       # explicit map (tests / programmatic)
            return self.urls.get(status) or self.urls.get("*")
        url = os.environ.get(_ENV.get(status, ""))
        if not url and status == "overquota":           # overquota shares the screen-out URL by default
            url = os.environ.get("SURVEY_REDIRECT_TERMINATED_URL")
        return (url or self.fallback or os.environ.get("SURVEY_REDIRECT_URL")
                or os.environ.get("SURVEY_DEFAULT_REDIRECT_URL"))

    def default_redirect(self, session, status="completed"):
        base = self._base(status)
        if not base:
            return {"kind": "default"}                   # nothing configured -> show the thank-you page
        uv = session.get("url_vars", {})
        params = {k: uv[k] for k in self.passback if k in uv}
        if params:
            base += ("&" if "?" in base else "?") + urlencode(params)
        return {"kind": "url", "url": base}
