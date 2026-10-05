"""mailchimp — a reference completion integration: upsert the respondent into a Mailchimp audience
when they finish, with merge fields + tags derived from their answers. Idempotent (keyed by the
email hash, so re-delivery is safe). A tag like `outcome:completed` can trigger a Customer Journey.

It's a worked example of the `Integration` seam — stdlib only (no SDK). Select it with
`SURVEY_INTEGRATION=mailchimp`. Config via env (per-survey mapping can also be passed as kwargs):

    MAILCHIMP_API_KEY     e.g. "abc123...-us21" (the `-usXX` datacenter suffix is parsed out)
    MAILCHIMP_LIST_ID     the audience / list id
    MAILCHIMP_DC          (optional) datacenter, if not derivable from the key
    MAILCHIMP_EMAIL_VAR   where the email is (default "email"): a url_var or an answer id
    MAILCHIMP_ON          "completed" (default) | "always" (also tag screen-outs, e.g. to suppress)
    MAILCHIMP_STATUS      status_if_new: "transactional" (default, no opt-in email) | "subscribed" | "pending"
    MAILCHIMP_TAGS        extra static tags, comma-separated
    MAILCHIMP_MERGE_MAP   JSON {MERGE_TAG: source}, source = "url:<var>" | "q:<qid>" | "var:<id>"
                          e.g. {"FNAME":"url:fname","AGE":"q:q_age","SEGMENT":"var:user_segment"}

**PII note:** this sends the respondent's email + the mapped answers to Mailchimp. Like `webhook`,
it's opt-in (you select it) and the deployer's call — disclose it in your privacy notice.
"""
import base64
import hashlib
import json
import os
import urllib.request

from survey_agent import env_select
from .base import Integration


def _csv(s):
    return [t.strip() for t in (s or "").split(",") if t.strip()]


def _json(s):
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


class MailchimpIntegration(Integration):
    def __init__(self, api_key=None, list_id=None, dc=None, on=None, email_var=None,
                 status_if_new=None, tags=None, merge_map=None, redirect=None):
        self.api_key = api_key or env_select("MAILCHIMP_API_KEY")
        self.list_id = list_id or env_select("MAILCHIMP_LIST_ID")
        self.dc = dc or env_select("MAILCHIMP_DC") or (self.api_key.split("-")[-1] if "-" in self.api_key else "")
        self.on = (on or env_select("MAILCHIMP_ON", "completed") or "completed").lower()
        self.email_var = email_var or env_select("MAILCHIMP_EMAIL_VAR", "email") or "email"
        self.status_if_new = status_if_new or env_select("MAILCHIMP_STATUS", "transactional") or "transactional"
        self.tags = tags if tags is not None else _csv(env_select("MAILCHIMP_TAGS"))
        self.merge_map = merge_map if merge_map is not None else _json(os.environ.get("MAILCHIMP_MERGE_MAP", ""))
        self.redirect = redirect or os.environ.get("SURVEY_DEFAULT_REDIRECT_URL")

    # ---- Integration contract ----
    def terminal(self, session, status, reason_code=None):
        if self.on != "always" and status != "completed":
            return                                       # default: only sync completes
        email = self._email(session)
        if not (email and self.api_key and self.list_id):
            return                                       # not configured / no email -> fail-open no-op
        sub = hashlib.md5(email.strip().lower().encode()).hexdigest()
        base = f"https://{self.dc}.api.mailchimp.com/3.0/lists/{self.list_id}/members/{sub}"
        self._request("PUT", base, {                     # upsert the contact (idempotent)
            "email_address": email,
            "status_if_new": self.status_if_new,
            "merge_fields": self._merge(session),
        })
        tags = self.tags + [f"survey:{session.get('survey_id')}", f"outcome:{status}"] + \
            ([f"reason:{reason_code}"] if reason_code else [])
        self._request("POST", base + "/tags",            # tags fire on new AND existing members
                      {"tags": [{"name": t, "status": "active"} for t in tags]})

    def default_redirect(self, session):
        return {"kind": "url", "url": self.redirect} if self.redirect else {"kind": "default"}

    # ---- helpers ----
    def _email(self, session):
        uv, ans = session.get("url_vars", {}) or {}, session.get("answers", {}) or {}
        return uv.get(self.email_var) or ans.get(self.email_var) or ans.get("q_email")

    def _merge(self, session):
        uv = session.get("url_vars", {}) or {}
        ans = session.get("answers", {}) or {}
        var = session.get("variables", {}) or {}
        out = {}
        for tag, src in self.merge_map.items():
            kind, _, key = str(src).partition(":")
            val = {"url": uv, "q": ans, "var": var}.get(kind, {}).get(key)
            if val is not None:
                out[tag] = "|".join(map(str, val)) if isinstance(val, list) else val
        return out

    def _request(self, method, url, body):
        """One Mailchimp Marketing API call. Best-effort + fail-open (prod: durable outbox + retry).
        Isolated so it's trivially mockable in tests — no network needed to verify the payloads."""
        data = json.dumps(body).encode()
        auth = base64.b64encode(f"survey-agent:{self.api_key}".encode()).decode()
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Basic {auth}", "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass
