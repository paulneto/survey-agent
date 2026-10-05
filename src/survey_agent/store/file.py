"""Flat-file store — JSON sessions + JSONL responses + JSON quota counters under a data dir.

Great for local/dev/single-box/low-volume and for keeping responses as inspectable, S3-syncable
files. Uses an OS file lock for quota increments so a single multi-threaded process is safe.
NOT safe across multiple processes/hosts at high concurrency — use the mongo backend for that.

Layout (data_dir, default ./survey_data):
  sessions/<sha1(session_id)>.json
  quotas/<survey_id>_v<version>.json        {quota_id: count}
  responses/<survey_id>.jsonl               append-only export records
"""
import hashlib
import json
import os
import threading

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

    # ---- quotas (atomic via file lock) ----
    def quota_counts(self, survey_id, version):
        return self._read_json(self._quota_path(survey_id, version), {})

    def incr_quota(self, survey_id, version, quota_id):
        path = self._quota_path(survey_id, version)
        with self._lock:
            # touch + lock the file for a read-modify-write
            f = open(path, "a+")
            try:
                if fcntl:
                    fcntl.flock(f, fcntl.LOCK_EX)
                f.seek(0)
                raw = f.read().strip()
                counts = json.loads(raw) if raw else {}
                counts[quota_id] = counts.get(quota_id, 0) + 1
                f.seek(0)
                f.truncate()
                f.write(json.dumps(counts))
                f.flush()
                return counts[quota_id]
            finally:
                if fcntl:
                    fcntl.flock(f, fcntl.LOCK_UN)
                f.close()

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

    def list_responses(self, survey_id=None, uid=None):
        out = []
        rdir = os.path.join(self.dir, "responses")
        files = ([f"{survey_id}.jsonl"] if survey_id else os.listdir(rdir))
        for name in files:
            path = os.path.join(rdir, name)
            if not os.path.exists(path):
                continue
            with open(path) as f:
                for line in f:
                    if line.strip():
                        r = json.loads(line)
                        if uid is None or str(r.get("url_variables", {}).get("uid")) == str(uid):
                            out.append(r)
        return out

    def delete_responses(self, uid=None, survey_id=None):
        """Erasure hook (DSAR): rewrite each responses file, dropping rows matching uid (and/or
        survey_id). Returns count removed. Atomic per file (write tmp, then os.replace)."""
        rdir = os.path.join(self.dir, "responses")
        if not os.path.isdir(rdir):
            return 0
        files = ([f"{survey_id}.jsonl"] if survey_id else os.listdir(rdir))
        removed = 0
        with self._lock:
            for name in files:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(rdir, name)
                if not os.path.exists(path):
                    continue
                kept = []
                with open(path) as f:
                    for line in f:
                        if not line.strip():
                            continue
                        r = json.loads(line)
                        if uid is not None and str(r.get("url_variables", {}).get("uid")) != str(uid):
                            kept.append(line if line.endswith("\n") else line + "\n")
                        else:
                            removed += 1
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    f.writelines(kept)
                os.replace(tmp, path)
        return removed
