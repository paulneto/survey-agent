"""In-process store — for tests and the ephemeral demo. Not for production (state is lost on
restart and not shared across instances)."""
import threading


class MemoryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}     # session_id -> session
        self._by_key = {}       # (survey_id, rid, uid) -> session_id
        self._quotas = {}       # (survey_id, version) -> {quota_id: count}
        self._responses = []    # list of export records

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

    def record_response(self, survey_id, response):
        with self._lock:
            self._responses.append(response)

    def list_responses(self, survey_id=None, uid=None):
        out = self._responses
        if survey_id is not None:
            out = [r for r in out if r.get("survey_id") == survey_id]
        if uid is not None:
            out = [r for r in out if str(r.get("url_variables", {}).get("uid")) == str(uid)]
        return list(out)

    def delete_responses(self, uid=None, survey_id=None):
        """Erasure hook (DSAR): drop responses matching uid and/or survey_id. Returns count removed."""
        def matches(r):
            return ((uid is None or str(r.get("url_variables", {}).get("uid")) == str(uid))
                    and (survey_id is None or r.get("survey_id") == survey_id))
        with self._lock:
            before = len(self._responses)
            self._responses = [r for r in self._responses if not matches(r)]
            return before - len(self._responses)
