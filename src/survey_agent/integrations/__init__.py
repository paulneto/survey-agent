"""Integrations — what happens on a survey's terminal events. The engine produces neutral terminal
events (engine/terminal.py); an Integration delivers them somewhere and decides the post-survey
redirect.

Built-ins (selected by SURVEY_INTEGRATION, or get_integration(kind=...)):
  none      capture only; no passback (default)
  redirect  browser-redirect passback: 302 the respondent to a per-status URL (SURVEY_REDIRECT_*_URL)
            with selected url_vars appended (SURVEY_REDIRECT_PASSBACK=uid,rid). A survey overrides a
            terminal with a spec redirect of kind `url`.
  webhook   POST the neutral event JSON to one URL (Zapier/n8n/your API/a panel)
  panel     POST each lifecycle event to a per-status URL (SURVEY_PANEL_*_URL), signed with
            SURVEY_PANEL_SHARED_SECRET — the generic panel/sample-provider passback (URLs are config)
  mailchimp reference CRM/ESP adapter — upsert the respondent into a Mailchimp audience with merge
            fields + tags derived from their answers (a worked example of the seam; stdlib only)

A panel/CRM with a bespoke wire format can still ship as a plugin and register via the
`survey_agent.integrations` entry-point group — `pip install` one and select it by name. New
panels/CRMs implement the `Integration` interface (integrations/base.py).
"""
import os

try:
    from importlib.metadata import entry_points
except Exception:  # pragma: no cover
    entry_points = None


def _plugins():
    out = {}
    if entry_points is None:
        return out
    try:
        for ep in entry_points(group="survey_agent.integrations"):
            out[ep.name] = ep
    except Exception:
        pass
    return out


def get_integration(kind=None, **kwargs):
    from survey_agent import env_select
    kind = ((kind or env_select("SURVEY_INTEGRATION", "none")) or "none").lower()
    if kind in ("none", ""):
        from .base import Integration
        return Integration()
    if kind == "redirect":
        from .redirect import RedirectIntegration
        return RedirectIntegration(**kwargs)
    if kind == "webhook":
        from .webhook import WebhookIntegration
        return WebhookIntegration(**kwargs)
    if kind == "panel":
        from .panel import PanelIntegration
        return PanelIntegration(**kwargs)
    if kind == "mailchimp":
        from .mailchimp import MailchimpIntegration
        return MailchimpIntegration(**kwargs)
    plugins = _plugins()
    if kind in plugins:
        return plugins[kind].load()(**kwargs)
    raise ValueError(f"unknown SURVEY_INTEGRATION: {kind!r} "
                     f"(built-ins: none|redirect|webhook|panel|mailchimp; or install a plugin that registers it)")
