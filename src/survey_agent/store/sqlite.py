"""SQLite store — a single-file, concurrency-safe backend that needs no server.

The middle rung between `file` and a networked database:

  file     simplest, inspectable JSON/JSONL; quota increments are only safe in ONE process.
  sqlite   ONE file, but quota increments are atomic and safe across threads AND processes (WAL
           mode + an atomic UPSERT) — so it's the right default once you run more than one worker
           on a single box, without standing up any database server.
  mongo    a reference multi-host backend (see store/mongo.py).

Pure stdlib (`sqlite3`); no dependency, no daemon. The whole dataset is one file you can copy,
back up, or `sqlite3 survey.db` into. Path via SURVEY_SQLITE_PATH (default
<SURVEY_DATA_DIR or ./survey_data>/survey.db).

Not a fit for: multiple hosts sharing storage (SQLite over NFS is unsafe) — use mongo or your own
store there. For a single box with N worker processes, it's exactly right.
"""
import contextlib
import json
import os
import sqlite3

from . import (check_reservation, erasure_result, norm_id_fields, response_matches,
               session_matches)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    data       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quotas (
    survey_id TEXT NOT NULL,
    version   INTEGER NOT NULL,
    quota_id  TEXT NOT NULL,
    count     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (survey_id, version, quota_id)
);
CREATE TABLE IF NOT EXISTS responses (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    survey_id TEXT NOT NULL,
    uid       TEXT,
    status    TEXT,
    data      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_responses_survey ON responses (survey_id);
CREATE INDEX IF NOT EXISTS idx_responses_survey_uid ON responses (survey_id, uid);
"""


class SQLiteStore:
    def __init__(self, path=None):
        self.path = os.path.abspath(
            path or os.environ.get("SURVEY_SQLITE_PATH")
            or os.path.join(os.environ.get("SURVEY_DATA_DIR", "./survey_data"), "survey.db"))
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _conn(self):
        # A fresh connection per operation: sqlite3 connections aren't safe to share across the
        # runtime's request threads, and WAL + busy_timeout let independent connections (and
        # processes) coordinate writes safely. timeout waits out a busy writer instead of erroring.
        # `with c:` commits on success / rolls back on error; the finally closes it deterministically
        # (don't rely on refcount GC — matters on PyPy and if a traceback retains the connection).
        c = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=10000")
        c.execute("PRAGMA foreign_keys=ON")
        try:
            with c:
                yield c
        finally:
            c.close()

    def ping(self):
        with self._conn() as c:
            c.execute("SELECT 1")

    # ---- sessions ----
    def get_or_create_session(self, survey_id, rid, uid, factory):
        session_id = f"{survey_id}:{rid}:{uid}"
        with self._conn() as c:
            row = c.execute("SELECT data FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is not None:
                return json.loads(row["data"]), False
            session = factory()
            # INSERT OR IGNORE makes the create idempotent under a race: if a concurrent request won,
            # re-read its row rather than clobbering it.
            cur = c.execute("INSERT OR IGNORE INTO sessions (session_id, data) VALUES (?, ?)",
                            (session["session_id"], json.dumps(session)))
            if cur.rowcount == 0:
                row = c.execute("SELECT data FROM sessions WHERE session_id=?",
                                (session["session_id"],)).fetchone()
                return json.loads(row["data"]), False
            return session, True

    def save_session(self, session):
        with self._conn() as c:
            c.execute("INSERT INTO sessions (session_id, data) VALUES (?, ?) "
                      "ON CONFLICT(session_id) DO UPDATE SET data=excluded.data",
                      (session["session_id"], json.dumps(session)))

    def get_session_by_id(self, session_id):
        with self._conn() as c:
            row = c.execute("SELECT data FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            return json.loads(row["data"]) if row else None

    # ---- quotas (atomic increment) ----
    def quota_counts(self, survey_id, version):
        with self._conn() as c:
            rows = c.execute("SELECT quota_id, count FROM quotas WHERE survey_id=? AND version=?",
                             (survey_id, version)).fetchall()
            return {r["quota_id"]: r["count"] for r in rows}

    def incr_quota(self, survey_id, version, quota_id):
        # One atomic UPSERT: the per-row increment can't lose a concurrent writer's count (unlike the
        # file store's read-modify-write), and WAL serializes writers across threads and processes.
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute("INSERT INTO quotas (survey_id, version, quota_id, count) VALUES (?, ?, ?, 1) "
                      "ON CONFLICT(survey_id, version, quota_id) DO UPDATE SET count=count+1",
                      (survey_id, version, quota_id))
            row = c.execute("SELECT count FROM quotas WHERE survey_id=? AND version=? AND quota_id=?",
                            (survey_id, version, quota_id)).fetchone()
            c.execute("COMMIT")
            return row["count"]

    def reserve_quotas(self, survey_id, version, limits):
        """All-or-nothing: increment every bucket in `limits` iff each limited one is below its limit.
        One BEGIN IMMEDIATE transaction (takes the write lock up front), so the check and the increments
        are atomic across threads and processes."""
        if not limits:
            return True
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                rows = c.execute("SELECT quota_id, count FROM quotas WHERE survey_id=? AND version=?",
                                 (survey_id, version)).fetchall()
                if not check_reservation({r["quota_id"]: r["count"] for r in rows}, limits):
                    c.execute("ROLLBACK")
                    return False
                for qid in limits:
                    c.execute("INSERT INTO quotas (survey_id, version, quota_id, count) VALUES (?, ?, ?, 1) "
                              "ON CONFLICT(survey_id, version, quota_id) DO UPDATE SET count=count+1",
                              (survey_id, version, qid))
                c.execute("COMMIT")
                return True
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise

    # ---- responses (append-only) ----
    def record_response(self, survey_id, response):
        uid = (response.get("url_variables") or {}).get("uid")
        with self._conn() as c:
            c.execute("INSERT INTO responses (survey_id, uid, status, data) VALUES (?, ?, ?, ?)",
                      (survey_id, None if uid is None else str(uid),
                       response.get("status"), json.dumps(response)))

    def count_responses_by_survey(self):
        out = {}
        with self._conn() as c:
            for r in c.execute("SELECT survey_id, status, COUNT(*) n FROM responses GROUP BY survey_id, status"):
                b = out.setdefault(r["survey_id"], {"complete": 0, "disqualified": 0, "total": 0})
                b["total"] += r["n"]
                if r["status"] == "Complete":
                    b["complete"] += r["n"]
                elif r["status"] == "Disqualified":
                    b["disqualified"] += r["n"]
        return out

    def list_responses(self, survey_id=None, uid=None, id_fields=None):
        """`uid` matches url var `uid` (the indexed column) by default; pass `id_fields` to match the id
        against any of those url-var names (custom respondent identity) — a scan, fine for DSAR."""
        fields = norm_id_fields(id_fields)
        clauses, params = [], []
        if survey_id is not None:
            clauses.append("survey_id=?")
            params.append(survey_id)
        if uid is not None and fields == ("uid",):
            clauses.append("uid=?")
            params.append(str(uid))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._conn() as c:
            rows = c.execute("SELECT data FROM responses" + where + " ORDER BY seq", params).fetchall()
            out = [json.loads(r["data"]) for r in rows]
        if uid is not None and fields != ("uid",):
            out = [r for r in out if response_matches(r, uid, fields)]
        return out

    def _matching(self, c, respondent_id, fields, survey_id):
        rq, rp = "SELECT seq, data FROM responses", []
        if survey_id is not None:
            rq, rp = rq + " WHERE survey_id=?", [survey_id]
        resp = [(r["seq"], json.loads(r["data"])) for r in c.execute(rq + " ORDER BY seq", rp)]
        resp = [(k, d) for k, d in resp if response_matches(d, respondent_id, fields)]
        sess = [(r["session_id"], json.loads(r["data"]))
                for r in c.execute("SELECT session_id, data FROM sessions")]
        sess = [(k, d) for k, d in sess if session_matches(d, respondent_id, fields)
                and (survey_id is None or d.get("survey_id") == survey_id)]
        return resp, sess

    def find_respondent(self, respondent_id, id_fields=("uid",), survey_id=None):
        with self._conn() as c:
            resp, sess = self._matching(c, respondent_id, norm_id_fields(id_fields), survey_id)
        return {"responses": [d for _, d in resp], "sessions": [d for _, d in sess]}

    def erase_respondent(self, respondent_id, id_fields=("uid",), survey_id=None):
        """DSAR erasure: delete every matching response AND session (partial sessions, retakes, every
        survey) in one transaction. Returns counts + the upload refs / webhook keys found in them."""
        fields = norm_id_fields(id_fields)
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                resp, sess = self._matching(c, respondent_id, fields, survey_id)
                c.executemany("DELETE FROM responses WHERE seq=?", [(k,) for k, _ in resp])
                c.executemany("DELETE FROM sessions WHERE session_id=?", [(k,) for k, _ in sess])
                c.execute("COMMIT")
            except BaseException:
                if c.in_transaction:
                    c.execute("ROLLBACK")
                raise
        return erasure_result([d for _, d in resp], [d for _, d in sess])

    def delete_responses(self, uid=None, survey_id=None):
        """Compat wrapper. With a uid this is a full respondent erasure (responses AND sessions, matched
        on url var `uid`); with only survey_id it drops that survey's responses. Returns responses removed."""
        if uid is not None:
            return self.erase_respondent(uid, ("uid",), survey_id)["responses"]
        where, params = (" WHERE survey_id=?", [survey_id]) if survey_id is not None else ("", [])
        with self._conn() as c:
            return c.execute("DELETE FROM responses" + where, params).rowcount
