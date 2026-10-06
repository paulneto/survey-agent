"""Terminal events — the generic, integration-agnostic mapping from a runtime terminal status to a
delivery 'event' and a neutral event payload.

This is pure and the platform-free: it knows the four event names and how a status maps to one, but it
does NOT know about any specific panel/CRM/webhook, env-var names, or `uid/rid`. Delivery lives in
the `integrations` package; the generic payload simply carries `url_variables` (whatever the link
passed) and integrations read what they need.
"""
import hashlib

from .runtime import (
    STATUS_COMPLETED, STATUS_DISQUALIFIED, STATUS_OVERQUOTA, STATUS_USER_TERMINATED,
    EVENT_STARTED, EVENT_COMPLETED, EVENT_TERMINATED, EVENT_TERMINATED_BY_USER,
    response_id,
)

# runtime terminal status -> delivery event
STATUS_TO_EVENT = {
    STATUS_COMPLETED: EVENT_COMPLETED,
    STATUS_DISQUALIFIED: EVENT_TERMINATED,
    STATUS_OVERQUOTA: EVENT_TERMINATED,
    STATUS_USER_TERMINATED: EVENT_TERMINATED_BY_USER,
}


def idempotency_key(response_id, status):
    return hashlib.sha256(f"{response_id}|{status}".encode()).hexdigest()


def passback_url_vars(session):
    """The captured url vars that may leave the system in a passback event: only the survey's
    declared `settings.url_variables` + identity vars (recorded on the session as `passback_vars` by
    `SurveyRuntime.new_session`). An incidental/sensitive query param on the invite link is still
    captured in the stored response's `url_variables` (the drop-in contract) but is never forwarded.
    Sessions without the allowlist (legacy/synthetic dicts) forward everything, as before."""
    uv = dict(session.get("url_vars", {}) or {})
    allow = session.get("passback_vars")
    if allow is None:
        return uv
    return {k: v for k, v in uv.items() if k in set(allow)}


def build_event(session, status, reason_code=None):
    """A neutral terminal/started event. `url_variables` carries the survey's declared url vars +
    identity vars from the survey_url (uid/rid/group/utm/…, see passback_url_vars); the engine
    hardcodes no param names. Integrations map this to their own wire format."""
    rid = response_id(session)
    return {
        "event": STATUS_TO_EVENT.get(status, status),
        "status": status,
        "reason_code": reason_code,
        "survey_id": session.get("survey_id"),
        "response_id": rid,
        "url_variables": passback_url_vars(session),
        "idempotency_key": idempotency_key(rid, status),
    }
