"""In-process store — for tests and the ephemeral demo. Not for production (state is lost on
restart and not shared across instances)."""
import threading

from . import (check_reservation, erasure_result, norm_id_fields, response_matches,
               session_matches)


class MemoryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}     # session_id -> session
        self._by_key = {}       # (survey_id, rid, uid) -> session_id
        self._quotas = {}       # (survey_id, version) -> {quota_id: count}
        self._responses = []    # list of export records
        self._reservations = {}  # (survey_id, version, key) -> bool (idempotent reserve_quotas)

    def get_or_create_session(self, survey_id, rid, uid, factory):
        key = (survey_id, str(rid), str(uid))
        with self._lock:
            sid = self._by_key.get(key)
            if sid is not None:
                return self._sessions[sid], False
            session = factory()
            self._sessions[session["session_id"]] = session
            self._by_key[key] = session["session_id"]
            return session, True

    def save_session(self, session):
        with self._lock:
            self._sessions[session["session_id"]] = session

    def get_session_by_id(self, session_id):
        return self._sessions.get(session_id)

    def quota_counts(self, survey_id, version):
        return dict(self._quotas.get((survey_id, version), {}))

    def incr_quota(self, survey_id, version, quota_id):
        with self._lock:
            counts = self._quotas.setdefault((survey_id, version), {})
            counts[quota_id] = counts.get(quota_id, 0) + 1
            return counts[quota_id]

    def reserve_quotas(self, survey_id, version, limits, key=None):
        """All-or-nothing: increment every bucket in `limits` iff each limited one is below its limit.
        With `key` (the response id) a repeated call returns the first call's result unchanged."""
        with self._lock:
            rkey = (survey_id, version, str(key)) if key is not None else None
            if rkey is not None and rkey in self._reservations:
                return self._reservations[rkey]
            counts = self._quotas.setdefault((survey_id, version), {})
            ok = check_reservation(counts, limits)
            if ok:
                for qid in (limits or {}):
                    counts[qid] = counts.get(qid, 0) + 1
            if rkey is not None:
                self._reservations[rkey] = ok
            return ok

    def record_response(self, survey_id, response):
        """Upsert keyed by the response `id` (a re-recorded terminal replaces, never duplicates).
        A record without an id is appended."""
        if "survey_id" not in response:            # list_responses filters on it (as mongo stores it)
            response = {"survey_id": survey_id, **response}
        with self._lock:
            rid = response.get("id")
            if rid is not None:
                for i, r in enumerate(self._responses):
                    if r.get("id") == rid:
                        self._responses[i] = response
                        return
            self._responses.append(response)

    def list_responses(self, survey_id=None, uid=None, id_fields=None):
        fields = norm_id_fields(id_fields)
        with self._lock:
            out = list(self._responses)
        if survey_id is not None:
            out = [r for r in out if r.get("survey_id") == survey_id]
        if uid is not None:
            out = [r for r in out if response_matches(r, uid, fields)]
        return out

    def find_respondent(self, respondent_id, id_fields=("uid",), survey_id=None):
        fields = norm_id_fields(id_fields)
        with self._lock:
            sessions = [dict(s) for s in self._sessions.values()
                        if session_matches(s, respondent_id, fields)
                        and (survey_id is None or s.get("survey_id") == survey_id)]
        return {"responses": self.list_responses(survey_id, uid=respondent_id, id_fields=fields),
                "sessions": sessions}

    def erase_respondent(self, respondent_id, id_fields=("uid",), survey_id=None):
        """DSAR erasure: drop every matching response AND session (partial, retakes, all surveys)."""
        fields = norm_id_fields(id_fields)
        with self._lock:
            gone_r = [r for r in self._responses if response_matches(r, respondent_id, fields)
                      and (survey_id is None or r.get("survey_id") == survey_id)]
            gone_ids = {id(r) for r in gone_r}
            self._responses = [r for r in self._responses if id(r) not in gone_ids]
            gone_s = [s for s in self._sessions.values() if session_matches(s, respondent_id, fields)
                      and (survey_id is None or s.get("survey_id") == survey_id)]
            for s in gone_s:
                self._sessions.pop(s["session_id"], None)
            ids = {s["session_id"] for s in gone_s}
            self._by_key = {k: v for k, v in self._by_key.items() if v not in ids}
        return erasure_result(gone_r, gone_s)

    def delete_responses(self, uid=None, survey_id=None):
        """Compat wrapper. With a uid this is a full respondent erasure (responses AND sessions, matched
        on url var `uid`); with only survey_id it drops that survey's responses. Returns responses removed."""
        if uid is not None:
            return self.erase_respondent(uid, ("uid",), survey_id)["responses"]
        with self._lock:
            before = len(self._responses)
            self._responses = [r for r in self._responses
                               if not (survey_id is None or r.get("survey_id") == survey_id)]
            return before - len(self._responses)
