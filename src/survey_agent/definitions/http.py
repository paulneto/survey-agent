"""HTTP-backed DefinitionStore — talks to the survey-agent API gateway instead of a direct DB.

Select with `SURVEY_DEFINITION_SOURCE=http`; configure `SURVEY_API_BASE` (default http://127.0.0.1:8001)
and `SURVEY_API_TOKEN`. Pure stdlib (urllib) on purpose: a console / agent / CLI becomes an HTTP client
with **no extra dependency** — only the gateway host installs `[api]`. This is how the console and the
MCP agent reach Mongo *through* an authenticated API instead of opening their own connection.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .base import DefinitionStore, check_survey_id, valid_survey_id


class HTTPDefinitionStore(DefinitionStore):
    def __init__(self, base=None, token=None, timeout=30):
        self.base = (base or os.environ.get("SURVEY_API_BASE") or "http://127.0.0.1:8001").rstrip("/")
        self.token = token or os.environ.get("SURVEY_API_TOKEN")
        self.timeout = timeout

    def _req(self, method, path, body=None, extra_headers=None):
        headers = {"Content-Type": "application/json", **(extra_headers or {})}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def _q(self, sid):
        return urllib.parse.quote(sid, safe="")

    def get_definition(self, survey_id, version=None):
        if not valid_survey_id(survey_id):
            return None
        suffix = f"?version={int(version)}" if version is not None else ""
        return self._req("GET", f"/v1/definitions/{self._q(survey_id)}{suffix}")

    def put_definition(self, spec):
        check_survey_id(((spec or {}).get("survey") or {}).get("id"))
        return (self._req("PUT", "/v1/definitions", body=spec) or {}).get("version")

    def get_registry(self, survey_id):
        if not valid_survey_id(survey_id):
            return None
        return self._req("GET", f"/v1/registry/{self._q(survey_id)}")

    def set_registry(self, survey_id, **fields):
        # Marking live is gated on the gateway too: forward the launch approval this process was given
        # (config.may_mark_live records it after checking `--approve` locally).
        check_survey_id(survey_id)
        from survey_agent.config import approved_launch_token
        tok = approved_launch_token()
        # The gateway gates any lifecycle write that touches status / live / live_version (anything that can
        # make or keep a survey live), so forward the approval whenever one of those is in the write.
        gated = any(k in fields for k in ("status", "live", "live_version"))
        extra = {"X-Survey-Launch-Approval": tok} if tok and gated else None
        return self._req("POST", f"/v1/registry/{self._q(survey_id)}", body=fields, extra_headers=extra)

    def list_surveys(self):
        return self._req("GET", "/v1/surveys") or []

    def iter_versions(self, survey_id):
        if not valid_survey_id(survey_id):
            return []
        return [tuple(v) for v in (self._req("GET", f"/v1/definitions/{self._q(survey_id)}/versions") or [])]

    def delete_definition(self, survey_id):
        check_survey_id(survey_id)
        self._req("DELETE", f"/v1/definitions/{self._q(survey_id)}")
        return True
