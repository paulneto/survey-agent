"""HTTP-backed response store (READ side) — the console / agent read responses through the API gateway
instead of a direct DB connection. Select with `SURVEY_STORE=http`; configure `SURVEY_API_BASE` +
`SURVEY_API_TOKEN`. Pure stdlib (urllib), so it adds no dependency.

Read-only by design: sessions / quota counters / response writes are the **runtime's** hot path and it
keeps a direct store. This backend serves the control plane (board counts, reporting, export) and raises
on write/session methods so a misconfiguration fails loud rather than silently dropping data.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request


class HTTPStore:
    def __init__(self, base=None, token=None, timeout=30):
        self.base = (base or os.environ.get("SURVEY_API_BASE") or "http://127.0.0.1:8001").rstrip("/")
        self.token = token or os.environ.get("SURVEY_API_TOKEN")
        self.timeout = timeout

    def _get(self, path):
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        req = urllib.request.Request(self.base + path, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    # ---- read side (control plane) ----
    def list_responses(self, survey_id=None, uid=None, id_fields=None):
        from . import norm_id_fields
        if norm_id_fields(id_fields) != ("uid",):
            raise NotImplementedError("SURVEY_STORE=http can only match a respondent on url var `uid`; "
                                      "run DSAR against the runtime's direct store")
        if not survey_id:
            raise NotImplementedError("SURVEY_STORE=http requires a survey_id to list responses")
        q = f"?uid={urllib.parse.quote(str(uid))}" if uid is not None else ""
        return self._get(f"/v1/responses/{urllib.parse.quote(survey_id, safe='')}{q}") or []

    def count_responses(self, survey_id):
        return self._get(f"/v1/responses/{urllib.parse.quote(survey_id, safe='')}/count")

    def count_responses_by_survey(self):
        return self._get("/v1/responses-counts") or {}

    # ---- write / session side: not served by the gateway (the runtime uses a direct store) ----
    def _read_only(self, *a, **k):
        raise NotImplementedError(
            "SURVEY_STORE=http is read-only (control plane). The respondent runtime must use a direct "
            "store (SURVEY_STORE=mongo/file) — it writes sessions/quotas/responses on the hot path.")

    get_or_create_session = save_session = get_session_by_id = _read_only
    record_response = incr_quota = quota_counts = reserve_quotas = _read_only
    # DSAR access/erasure needs sessions + every survey — the runtime's direct store, not the gateway.
    find_respondent = erase_respondent = delete_responses = _read_only
