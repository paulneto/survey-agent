"""Flat-file store — JSON sessions + JSONL responses + JSON quota counters under a data dir.

Great for local/dev/single-box/low-volume and for keeping responses as inspectable, S3-syncable
files. Quota updates (`incr_quota`, `reserve_quotas`) hold a thread lock plus an OS file lock (fcntl,
POSIX) around the read-check-write, so they're safe across threads and — on POSIX, on a local disk —
across processes on one box. Sessions/responses are not locked across processes: for multiple workers
use sqlite (one box) or mongo / your own store (many hosts).

Layout (data_dir, default ./survey_data):
  sessions/<sha1(session_id)>.json
  quotas/<survey_id>_v<version>.json        {quota_id: count}
  responses/<survey_id>.jsonl               append-only export records
"""
import contextlib
import hashlib
import json
import os
import threading

from . import (check_reservation, erasure_result, norm_id_fields, response_matches,
               session_matches)

try:
    import fcntl  # POSIX file locking
except ImportError:  # pragma: no cover - Windows
    fcntl = None


class FileStore:
    def __init__(self, data_dir="./survey_data"):
        self.dir = os.path.abspath(data_dir)
        for sub in ("sessions", "quotas", "responses"):
            os.makedirs(os.path.join(self.dir, sub), exist_ok=True)
        self._lock = threading.Lock()

    # ---- paths ----
    def _session_path(self, session_id):
        h = hashlib.sha1(session_id.encode()).hexdigest()
        return os.path.join(self.dir, "sessions", f"{h}.json")

    def _quota_path(self, survey_id, version):
        return os.path.join(self.dir, "quotas", f"{survey_id}_v{version}.json")

    def _responses_path(self, survey_id):
        return os.path.join(self.dir, "responses", f"{survey_id}.jsonl")

    def _read_json(self, path, default):
        if not os.path.exists(path):
            return default
        with open(path) as f:
            return json.load(f)

    def _write_json(self, path, obj):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(obj, f, indent=2)
        os.replace(tmp, path)

    # ---- sessions ----
    def get_or_create_session(self, survey_id, rid, uid, factory):
        session_id = f"{survey_id}:{rid}:{uid}"
        path = self._session_path(session_id)
        with self._lock:
            if os.path.exists(path):
                return self._read_json(path, None), False
            session = factory()
            self._write_json(self._session_path(session["session_id"]), session)
            return session, True

    def save_session(self, session):
        self._write_json(self._session_path(session["session_id"]), session)

    def get_session_by_id(self, session_id):
        return self._read_json(self._session_path(session_id), None)

    # ---- quotas (atomic via thread lock + OS file lock) ----
    @contextlib.contextmanager
    def _locked(self, path, exclusive=True):
        """Open (creating) `path` and hold an fcntl lock on it. Without fcntl (non-POSIX) this degrades
        to the in-process thread lock the callers already hold — safe within one process only."""
        f = open(path, "a+")
        try:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield f
        finally:
            try:
                if fcntl:
                    fcntl.flock(f, fcntl.LOCK_UN)
            finally:
                f.close()

    @staticmethod
    def _read_counts(f):
        f.seek(0)
        raw = f.read().strip()
        return json.loads(raw) if raw else {}

    @staticmethod
    def _write_counts(f, counts):
        f.seek(0)
        f.truncate()
        f.write(json.dumps(counts))
        f.flush()
        os.fsync(f.fileno())

    def quota_counts(self, survey_id, version):
        path = self._quota_path(survey_id, version)
        if not os.path.exists(path):
            return {}
        with self._locked(path, exclusive=False) as f:   # never observe a half-written file
            return self._read_counts(f)

    def incr_quota(self, survey_id, version, quota_id):
        with self._lock, self._locked(self._quota_path(survey_id, version)) as f:
            counts = self._read_counts(f)
            counts[quota_id] = counts.get(quota_id, 0) + 1
            self._write_counts(f, counts)
            return counts[quota_id]

    def reserve_quotas(self, survey_id, version, limits):
        """All-or-nothing: increment every bucket in `limits` iff each limited one is below its limit.
        One locked read-check-write of the survey-version's counter file."""
        if not limits:
            return True
        with self._lock, self._locked(self._quota_path(survey_id, version)) as f:
            counts = self._read_counts(f)
            if not check_reservation(counts, limits):
                return False
            for qid in limits:
                counts[qid] = counts.get(qid, 0) + 1
            self._write_counts(f, counts)
            return True

    # ---- responses (append-only) ----
    def record_response(self, survey_id, response):
        with self._lock:
            with open(self._responses_path(survey_id), "a") as f:
                f.write(json.dumps(response) + "\n")

    def count_responses_by_survey(self):
        """Status tallies for every survey in one pass over the response files (the id is the filename
        stem) — so the console board doesn't read each survey's file through a separate call."""
        out = {}
        rdir = os.path.join(self.dir, "responses")
        if not os.path.isdir(rdir):
            return out
        for name in os.listdir(rdir):
            if not name.endswith(".jsonl"):
                continue
            b = out.setdefault(name[: -len(".jsonl")], {"complete": 0, "disqualified": 0, "total": 0})
            with open(os.path.join(rdir, name)) as f:
                for line in f:
                    if not line.strip():
                        continue
                    st = json.loads(line).get("status")
                    b["total"] += 1
                    if st == "Complete":
                        b["complete"] += 1
                    elif st == "Disqualified":
                        b["disqualified"] += 1
        return out

    def _response_files(self, survey_id=None):
        rdir = os.path.join(self.dir, "responses")
        if not os.path.isdir(rdir):
            return []
        names = [f"{survey_id}.jsonl"] if survey_id else sorted(os.listdir(rdir))
        return [os.path.join(rdir, n) for n in names
                if n.endswith(".jsonl") and os.path.exists(os.path.join(rdir, n))]

    def list_responses(self, survey_id=None, uid=None, id_fields=None):
        fields = norm_id_fields(id_fields)
        out = []
        for path in self._response_files(survey_id):
            with open(path) as f:
                for line in f:
                    if line.strip():
                        r = json.loads(line)
                        if uid is None or response_matches(r, uid, fields):
                            out.append(r)
        return out

    def _iter_sessions(self):
        sdir = os.path.join(self.dir, "sessions")
        for name in sorted(os.listdir(sdir)) if os.path.isdir(sdir) else []:
            if not name.endswith(".json"):
                continue
            path = os.path.join(sdir, name)
            try:
                with open(path) as f:
                    yield path, json.load(f)
            except (OSError, ValueError):
                continue

    def find_respondent(self, respondent_id, id_fields=("uid",), survey_id=None):
        fields = norm_id_fields(id_fields)
        sessions = [s for _, s in self._iter_sessions() if session_matches(s, respondent_id, fields)
                    and (survey_id is None or s.get("survey_id") == survey_id)]
        return {"responses": self.list_responses(survey_id, uid=respondent_id, id_fields=fields),
                "sessions": sessions}

    def _drop_responses(self, keep, survey_id=None):
        """Rewrite each responses file keeping rows where keep(row) is true; returns the dropped rows.
        Atomic per file (write tmp, then os.replace). Caller holds self._lock."""
        dropped = []
        for path in self._response_files(survey_id):
            kept = []
            with open(path) as f:
                for line in f:
                    if not line.strip():
                        continue
                    r = json.loads(line)
                    if keep(r):
                        kept.append(line if line.endswith("\n") else line + "\n")
                    else:
                        dropped.append(r)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.writelines(kept)
            os.replace(tmp, path)
        return dropped

    def erase_respondent(self, respondent_id, id_fields=("uid",), survey_id=None):
        """DSAR erasure: delete every matching response AND session file (partial sessions, retakes,
        every survey). Returns counts + the upload refs / webhook keys found in what was deleted."""
        fields = norm_id_fields(id_fields)
        with self._lock:
            gone_r = self._drop_responses(lambda r: not response_matches(r, respondent_id, fields),
                                          survey_id)
            gone_s = []
            for path, s in self._iter_sessions():
                if session_matches(s, respondent_id, fields) and (
                        survey_id is None or s.get("survey_id") == survey_id):
                    os.remove(path)
                    gone_s.append(s)
        return erasure_result(gone_r, gone_s)

    def delete_responses(self, uid=None, survey_id=None):
        """Compat wrapper. With a uid this is a full respondent erasure (responses AND sessions, matched
        on url var `uid`); with only survey_id it drops that survey's responses. Returns responses removed."""
        if uid is not None:
            return self.erase_respondent(uid, ("uid",), survey_id)["responses"]
        with self._lock:
            return len(self._drop_responses(lambda r: False, survey_id))
