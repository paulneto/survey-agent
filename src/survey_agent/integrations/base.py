"""The Integration interface. Subclasses decide what happens on terminal events and where the
respondent is sent afterward. The base class is the `none` integration (capture only)."""


class Integration:
    def started(self, session):
        """Fired once when a respondent begins (after the first page is reached)."""

    def terminal(self, session, status, reason_code=None):
        """Fired on complete / disqualify / overquota / user_terminated."""

    def default_redirect(self, session, status="completed"):
        """Where to send the respondent at a terminal (`status`) when the survey doesn't set an explicit
        route/url. Base = nowhere (show the thank-you page); the `redirect` integration overrides this."""
        return {"kind": "default"}
