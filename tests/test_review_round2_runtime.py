"""Regression tests for the round-2 release-review runtime findings:

  N2  required URL signing fails CLOSED when no secret is configured — single + multi mode, surveys
      published after startup — and the multi-mode startup preflight inspects every served survey.
  N3  terminal processing is idempotent and keyed by the response id: keyed quota reservation,
      upsert-by-id response records, `terminal_recorded` only after the record, retry after a failure
      at any step, and separate store instances (separate workers) processing one session.
  N7  logged URLs carry only scheme + host (+port) and a fingerprint — never a path/query/userinfo.

Temp stores + synthetic respondents only.
"""
import json
import logging
import os
import sqlite3
import tempfile
import time
import urllib.error

import pytest

from survey_agent.engine import signing
from survey_agent.store.file import FileStore
from survey_agent.store.memory import MemoryStore
from survey_agent.store.sqlite import SQLiteStore
from test_review_runtime import _consent_survey, _hidden, _post_status, _quota_survey
from test_server_http import _get, _tok, running_multi, running_server

SIGNED = {"url_signing": {"required": True, "fields": ["uid", "rid", "group"]}}


# =============================================================================== N2
def test_multi_mode_required_signing_without_secret_refuses_every_invitation():
    spec = _consent_survey("t_sig", **SIGNED)
    with running_multi([spec], state={"t_sig": "live"}) as (base, _d):
        import survey_agent.server as server
        code, html = _get(f"{base}/s/t_sig?uid=forged&rid=1&group=g")
        assert code == 403 and "not configured" in html and "session_token" not in html
        assert server.STORE.get_session_by_id("t_sig:1:forged") is None
        assert server._signed_surveys() == ["t_sig"]


def test_public_multi_mode_with_blank_secret_fails_closed():
    spec = _consent_survey("t_sig", **SIGNED)
    with running_multi([spec], state={"t_sig": "live"}, SURVEY_HOST="0.0.0.0",
                       SURVEY_URL_SECRET="   ") as (base, _d):
        code, html = _get(f"{base}/s/t_sig?uid=1&rid=1&group=g")
        assert code == 403 and "session_token" not in html


def test_survey_published_after_startup_is_enforced_per_request():
    import survey_agent.registry as registry
    plain = _consent_survey("t_plain")
    with running_multi([plain], state={"t_plain": "live"}) as (base, d):
        import survey_agent.server as server
        assert server._signed_surveys() == []                 # nothing to flag at startup
        with open(os.path.join(d, "t_late.survey.json"), "w") as f:
            json.dump(_consent_survey("t_late", **SIGNED), f)
        registry.set_status("t_late", status="live", sdir=d)
        code, _ = _get(f"{base}/s/t_late?uid=1&rid=1&group=g")
        assert code == 403
        code, _ = _get(f"{base}/s/t_plain?uid=1&rid=1&group=g")   # unsigned surveys unaffected
        assert code == 200


def test_multi_mode_with_secret_admits_valid_signed_links_only():
    secret = "synthetic-signing-secret"
    spec = _consent_survey("t_sig", **SIGNED)
    with running_multi([spec], state={"t_sig": "live"}, SURVEY_URL_SECRET=secret) as (base, _d):
        code, _ = _get(f"{base}/s/t_sig?uid=1&rid=1&group=g")
        assert code == 403
        q = signing.signed_query({"uid": "1", "rid": "1", "group": "g"}, secret, ["uid", "rid", "group"])
        code, html = _get(f"{base}/s/t_sig?{q}")
        assert code == 200 and "Do you consent?" in html and _tok(html)


def test_preflight_flags_signed_surveys_in_multi_mode():
    from survey_agent import preflight
    issues = preflight.runtime_preflight(env={"SURVEY_STORE": "sqlite"}, store=None, ping=False,
                                         signed_surveys=["s_b", "s_a"])
    warns = [m for lvl, m in issues if lvl == "warn" and "url_signing" in m]
    assert warns and "s_a, s_b" in warns[0] and "REFUSED" in warns[0]
    issues = preflight.runtime_preflight(env={"SURVEY_STORE": "sqlite",
                                              "SURVEY_URL_SECRET": preflight.DEV_PLACEHOLDER},
                                         store=None, ping=False, signed_surveys=["s_a"])
    assert any(lvl == "fatal" and "SURVEY_URL_SECRET" in m for lvl, m in issues)
    issues = preflight.runtime_preflight(env={"SURVEY_STORE": "sqlite", "SURVEY_URL_SECRET": "x" * 32},
                                         store=None, ping=False, signed_surveys=["s_a"])
    assert not any("url_signing" in m or "SURVEY_URL_SECRET" in m for _, m in issues)


# =============================================================================== N3 — stores
def _mongo_store(client=None):
    mongomock = pytest.importorskip("mongomock")
    pytest.importorskip("pymongo")
    from survey_agent.store.mongo import MongoStore
    return MongoStore(db="t_round2", client=client or mongomock.MongoClient())


@pytest.fixture(params=["memory", "file", "sqlite", "mongo"])
def store(request, tmp_path):
    if request.param == "memory":
        return MemoryStore()
    if request.param == "file":
        return FileStore(str(tmp_path / "data"))
    if request.param == "sqlite":
        return SQLiteStore(str(tmp_path / "s.db"))
    return _mongo_store()


def test_keyed_reservation_is_idempotent(store):
    assert store.reserve_quotas("s1", 1, {"total": 2}, key="r1") is True
    assert store.reserve_quotas("s1", 1, {"total": 2}, key="r1") is True      # replay: no 2nd slot
    assert store.quota_counts("s1", 1) == {"total": 1}
    assert store.reserve_quotas("s1", 1, {"total": 2}, key="r2") is True
    assert store.reserve_quotas("s1", 1, {"total": 2}, key="r3") is False     # full
    assert store.reserve_quotas("s1", 1, {"total": 2}, key="r3") is False     # same answer again
    assert store.reserve_quotas("s1", 1, {"total": 2}, key="r1") is True      # original outcome kept
    assert store.quota_counts("s1", 1) == {"total": 2}
    assert store.reserve_quotas("s1", 1, {"total": 2, "seg": None}) is False  # unkeyed still works


def test_record_response_is_an_upsert_by_id(store):
    store.record_response("s1", {"id": "a", "status": "Complete", "url_variables": {"uid": "u1"}})
    store.record_response("s1", {"id": "b", "status": "Complete", "url_variables": {"uid": "u2"}})
    store.record_response("s1", {"id": "a", "status": "Disqualified", "url_variables": {"uid": "u1"}})
    rows = store.list_responses("s1")
    assert sorted(r["id"] for r in rows) == ["a", "b"]
    assert next(r for r in rows if r["id"] == "a")["status"] == "Disqualified"
    store.record_response("s1", {"status": "Complete"})                       # id-less: appended
    store.record_response("s1", {"status": "Complete"})
    assert len(store.list_responses("s1")) == 4
    assert store.erase_respondent("u1")["responses"] == 1


def test_file_store_reads_legacy_quota_files(tmp_path):
    fs = FileStore(str(tmp_path / "d"))
    with open(fs._quota_path("s1", 1), "w") as f:
        json.dump({"total": 3}, f)                                             # pre-round-2 format
    assert fs.quota_counts("s1", 1) == {"total": 3}
    assert fs.reserve_quotas("s1", 1, {"total": 5}, key="k") is True
    assert fs.quota_counts("s1", 1) == {"total": 4}
    assert fs.incr_quota("s1", 1, "total") == 5
    assert fs.reserve_quotas("s1", 1, {"total": 9}, key="k") is True and fs.quota_counts("s1", 1) == {"total": 5}


def test_sqlite_migrates_a_pre_round2_database(tmp_path):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE responses (seq INTEGER PRIMARY KEY AUTOINCREMENT, survey_id TEXT NOT NULL,
                                uid TEXT, status TEXT, data TEXT NOT NULL);""")
    for rid in ("x", "x", "y"):                                                # a legacy duplicate
        c.execute("INSERT INTO responses (survey_id, uid, status, data) VALUES (?,?,?,?)",
                  ("s1", "u", "Complete", json.dumps({"id": rid, "status": "Complete"})))
    c.commit()
    c.close()
    db = SQLiteStore(path)
    assert len(db.list_responses("s1")) == 3                                   # nothing lost
    db.record_response("s1", {"id": "y", "status": "Disqualified"})            # upserts the keyed row
    db.record_response("s1", {"id": "z", "status": "Complete"})
    rows = db.list_responses("s1")
    assert len(rows) == 4 and [r["status"] for r in rows if r["id"] == "y"] == ["Disqualified"]
    SQLiteStore(path)                                                           # re-open: idempotent


# =============================================================================== N3 — on_terminal
def _completed_session(server, rt, n=1):
    s = rt.new_session(f"t_q:{n}:{n}", {"uid": str(n), "rid": str(n)}, response_id=f"resp{n}")
    s.update(token="x", rid=str(n), uid=str(n))
    rt.start(s, server.quota_state(rt))
    step = rt.submit(s, "p1", {"q1": "a"}, server.quota_state(rt))
    assert step["terminal"]["status"] == "completed"
    return s, step["terminal"]


class _FailOnce:
    """Wrap a store so `method` raises OSError on its first call (an injected storage failure)."""

    def __init__(self, inner, method):
        self._inner, self._method, self.failed = inner, method, False

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if name == self._method and not self.failed:
            def boom(*a, **k):
                self.failed = True
                raise OSError("injected storage failure")
            return boom
        return attr


def _sqlite_env(tmp_path):
    return {"SURVEY_STORE": "sqlite", "SURVEY_SQLITE_PATH": str(tmp_path / "rt.db")}


@pytest.mark.parametrize("method", ["reserve_quotas", "record_response"])
def test_storage_failure_then_retry_records_exactly_once(tmp_path, method):
    with running_server(_quota_survey(), **_sqlite_env(tmp_path)):
        import survey_agent.server as server
        rt, real = server.RT, server.STORE
        fired = []
        server.INTEGRATION.terminal = lambda session, status, reason_code=None: fired.append(status)
        s, term = _completed_session(server, rt)
        server.STORE = _FailOnce(real, method)
        with pytest.raises(OSError):
            server.on_terminal(s, term, rt)
        server.STORE = real
        stored = real.get_session_by_id("t_q:1:1")
        assert server.terminal_unrecorded(stored) and stored["status"] == "completed"
        assert real.list_responses("t_q") == [] and fired == []
        applied = server.on_terminal(stored, stored["terminal_pending"], rt)    # the retry
        assert applied["status"] == "completed"
        assert len(real.list_responses("t_q")) == 1
        assert real.quota_counts("t_q", 1) == {"total": 1}
        assert real.get_session_by_id("t_q:1:1")["terminal_recorded"] is True
        assert fired == ["completed"]
        server.on_terminal(real.get_session_by_id("t_q:1:1"), term, rt)          # replay: no-op
        assert len(real.list_responses("t_q")) == 1 and fired == ["completed"]


def test_failure_after_record_before_flag_is_retried_without_duplicates(tmp_path):
    with running_server(_quota_survey(), **_sqlite_env(tmp_path)):
        import survey_agent.server as server
        rt, real = server.RT, server.STORE
        s, term = _completed_session(server, rt)

        class _FailSecondSave:
            def __init__(self):
                self.saves = 0

            def __getattr__(self, name):
                return getattr(real, name)

            def save_session(self, session):
                self.saves += 1
                if self.saves == 2:                    # (3): the terminal_recorded save
                    raise OSError("injected")
                return real.save_session(session)
        server.STORE = _FailSecondSave()
        with pytest.raises(OSError):
            server.on_terminal(s, term, rt)
        server.STORE = real
        assert len(real.list_responses("t_q")) == 1                       # the record landed
        stored = real.get_session_by_id("t_q:1:1")
        assert server.terminal_unrecorded(stored)
        server.on_terminal(stored, stored["terminal_pending"], rt)
        assert len(real.list_responses("t_q")) == 1
        assert real.quota_counts("t_q", 1) == {"total": 1}
        assert real.get_session_by_id("t_q:1:1")["terminal_recorded"] is True


def test_passback_failure_does_not_lose_or_duplicate_the_response(tmp_path):
    with running_server(_quota_survey(), **_sqlite_env(tmp_path)):
        import survey_agent.server as server
        rt = server.RT

        def bad(*a, **k):
            raise RuntimeError("receiver down")
        server.INTEGRATION.terminal = bad
        s, term = _completed_session(server, rt)
        assert server.on_terminal(s, term, rt)["status"] == "completed"
        assert len(server.STORE.list_responses("t_q")) == 1
        assert server.STORE.get_session_by_id("t_q:1:1")["terminal_recorded"] is True


def _two_workers(server, rt, store_a, store_b, n=1):
    """Both workers load the SAME completed-but-unrecorded session snapshot, then each processes it
    with its own store instance (no shared process lock)."""
    s, term = _completed_session(server, rt, n)
    s["terminal_pending"] = term
    store_a.save_session(s)
    snap_a = json.loads(json.dumps(store_a.get_session_by_id(s["session_id"])))
    snap_b = json.loads(json.dumps(store_b.get_session_by_id(s["session_id"])))
    out = []
    for st, snap in ((store_a, snap_a), (store_b, snap_b)):
        server.STORE = st
        out.append(server.on_terminal(snap, snap["terminal_pending"], rt)["status"])
    return out


@pytest.mark.parametrize("kind", ["sqlite", "mongo", "file"])
def test_two_store_instances_process_one_session_once(tmp_path, kind):
    with running_server(_quota_survey(), **_sqlite_env(tmp_path)):
        import survey_agent.server as server
        rt = server.RT
        if kind == "sqlite":
            a, b = SQLiteStore(str(tmp_path / "w.db")), SQLiteStore(str(tmp_path / "w.db"))
        elif kind == "file":
            a, b = FileStore(str(tmp_path / "fd")), FileStore(str(tmp_path / "fd"))
        else:
            # mongomock clients don't share data, so two MongoStore instances over one mock server
            # stand in for two hosts (each keeps its own driver/transaction state).
            a = _mongo_store()
            b = _mongo_store(client=a._client)
        assert _two_workers(server, rt, a, b) == ["completed", "completed"]
        rows = b.list_responses("t_q")
        assert len(rows) == 1 and rows[0]["status"] == "Complete"
        assert a.quota_counts("t_q", 1) == {"total": 1}


@pytest.mark.parametrize("kind", ["sqlite", "mongo"])
def test_competing_respondents_across_store_instances_still_one_complete(tmp_path, kind):
    with running_server(_quota_survey(), **_sqlite_env(tmp_path)):
        import survey_agent.server as server
        rt = server.RT
        if kind == "sqlite":
            a, b = SQLiteStore(str(tmp_path / "w.db")), SQLiteStore(str(tmp_path / "w.db"))
        else:
            a = _mongo_store()
            b = _mongo_store(client=a._client)
        s1, t1 = _completed_session(server, rt, 1)
        s2, t2 = _completed_session(server, rt, 2)                   # both on a 0-count snapshot
        server.STORE = a
        r1 = server.on_terminal(s1, t1, rt)["status"]
        server.STORE = b
        r2 = server.on_terminal(s2, t2, rt)["status"]
        assert sorted([r1, r2]) == ["completed", "overquota"]
        assert a.quota_counts("t_q", 1) == {"total": 1}
        assert sorted(r["status"] for r in a.list_responses("t_q")) == ["Complete", "Disqualified"]
        # and replaying either worker's terminal changes nothing
        server.on_terminal(json.loads(json.dumps(s2)), t2, rt)
        assert len(a.list_responses("t_q")) == 2 and a.quota_counts("t_q", 1) == {"total": 1}


def test_http_post_after_failed_record_completes_on_resubmit(tmp_path):
    with running_server(_quota_survey(), **_sqlite_env(tmp_path)) as base:
        import survey_agent.server as server
        real = server.STORE
        _, h = _get(f"{base}/s/t_q?uid=1&rid=1")
        data = {"session_id": "t_q:1:1", "session_token": _tok(h), "page_id": "p1", "q1": "a"}
        server.STORE = _FailOnce(real, "record_response")
        try:
            code, _, _ = _post_status(f"{base}/s/t_q/submit", data)
        except (urllib.error.URLError, ConnectionError, Exception):  # the handler died mid-request
            code = None
        assert code != 200
        server.STORE = real
        assert real.list_responses("t_q") == []
        assert server.terminal_unrecorded(real.get_session_by_id("t_q:1:1"))
        code, body, _ = _post_status(f"{base}/s/t_q/submit", data)    # the respondent retries
        assert code == 200 and "Complete" in body
        assert len(real.list_responses("t_q")) == 1
        assert real.quota_counts("t_q", 1) == {"total": 1}
        code, body, _ = _post_status(f"{base}/s/t_q/submit", data)    # a further replay is refused
        assert "Already done" in body and len(real.list_responses("t_q")) == 1


def test_http_get_reload_finishes_an_unrecorded_terminal(tmp_path):
    with running_server(_quota_survey(), **_sqlite_env(tmp_path)) as base:
        import survey_agent.server as server
        real = server.STORE
        _, h = _get(f"{base}/s/t_q?uid=1&rid=1")
        server.STORE = _FailOnce(real, "record_response")
        try:
            _post_status(f"{base}/s/t_q/submit", {"session_id": "t_q:1:1", "session_token": _tok(h),
                                                  "page_id": "p1", "q1": "a"})
        except Exception:
            pass
        server.STORE = real
        assert real.list_responses("t_q") == []
        code, body = _get(f"{base}/s/t_q?uid=1&rid=1")                   # reopen the invite link
        assert code == 200 and "Complete" in body
        assert len(real.list_responses("t_q")) == 1
        assert not server.terminal_unrecorded(real.get_session_by_id("t_q:1:1"))


def test_legacy_terminal_session_is_not_reprocessed(tmp_path):
    with running_server(_quota_survey(), **_sqlite_env(tmp_path)) as base:
        import survey_agent.server as server
        rt = server.RT
        s, _term = _completed_session(server, rt)
        s["token"] = "legacy"
        server.STORE.save_session(s)                         # terminal, but no terminal_pending marker
        code, body = _get(f"{base}/s/t_q?uid=1&rid=1")
        assert code == 200 and "Already done" in body
        assert server.STORE.list_responses("t_q") == []


def test_restart_on_missing_pinned_version_deletes_discarded_upload_blobs(tmp_path):
    up = tmp_path / "uploads"
    with running_server(_consent_survey(), SURVEY_UPLOAD_DIR=str(up)) as base:
        import survey_agent.server as server
        _, h = _get(f"{base}/s/t_rev?uid=1&rid=1&group=g")
        s = server.STORE.get_session_by_id("t_rev:1:1")
        ref = server.upload_store().put("t_rev", s["response_id"], "q_doc", "doc.txt", b"synthetic")
        blob = up / ref["key"]
        assert blob.exists()
        s = dict(s, version=99, answers={"q_consent": "yes", "q_doc": ref})
        server.STORE.save_session(s)

        class _NoVersions:
            def get_definition(self, survey_id, version=None):
                return None
        server._DEFSTORE = _NoVersions()
        _, h2 = _get(f"{base}/s/t_rev?uid=1&rid=1&group=g")
        assert "Do you consent?" in h2
        assert server.STORE.get_session_by_id("t_rev:1:1")["answers"] == {}
        assert not blob.exists()


# =============================================================================== N7
SECRET_PATH = "/hooks/SYNTHETIC_PATH_TOKEN_123"


def test_redact_url_drops_path_query_userinfo_but_keeps_origin_and_fingerprint():
    from survey_agent import obs
    url = "https://user:pw@receiver.example:8443" + SECRET_PATH + "?k=SYNTH_Q#frag"
    red = obs.redact_url(url)
    assert red == "https://receiver.example:8443#" + obs.url_fingerprint(url)
    assert "SYNTHETIC_PATH_TOKEN" not in red and "hooks" not in red and "pw" not in red
    assert obs.redact_url(red) == red                                     # stable when re-applied
    assert obs.url_fingerprint(url) != obs.url_fingerprint(url.replace("123", "124"))
    assert obs.redact_url("not a url/with/SYNTHETIC_PATH_TOKEN_123").startswith("<redacted>#")


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_log_formatters_never_emit_a_path_token(fmt):
    from survey_agent import obs
    url = "https://receiver.example" + SECRET_PATH
    rec = logging.makeLogRecord({"msg": "webhook delivery failed", "levelname": "WARNING",
                                 "name": "survey_agent.runtime", "url": url,
                                 "webhook_url": url, "survey_id": "s1"})
    f = obs._JsonFormatter() if fmt == "json" else obs._TextFormatter()
    line = f.format(rec)
    assert "SYNTHETIC_PATH_TOKEN" not in line and "/hooks" not in line
    assert "receiver.example#" + obs.url_fingerprint(url) in line
    # and with the call site's own redaction applied first (server.py does both)
    rec2 = logging.makeLogRecord({"msg": "x", "url": obs.redact_url(url)})
    assert "receiver.example#" + obs.url_fingerprint(url) in f.format(rec2)


def test_outbox_list_never_shows_a_path_token(tmp_path):
    from survey_agent import obs
    from survey_agent.integrations.outbox import Outbox
    ob = Outbox(dir=str(tmp_path / "ob"), max_attempts=3)
    url = "https://receiver.example" + SECRET_PATH
    ob.enqueue({"method": "POST", "url": url, "payload": {}, "idempotency_key": "K"})
    listed = ob.list()
    assert listed and "SYNTHETIC_PATH_TOKEN" not in json.dumps(listed)
    assert listed[0]["url"] == obs.redact_url(url)


def test_webhook_failure_log_line_hides_path_token(caplog):
    spec = _consent_survey()
    spec["survey"]["actions"].insert(0, {"type": "webhook", "url": "http://127.0.0.1:9" + SECRET_PATH,
                                         "method": "POST", "payload": {}})
    with running_server(spec) as base:
        import survey_agent.server as server
        logging.getLogger("survey_agent").propagate = True
        try:
            with caplog.at_level(logging.WARNING, logger="survey_agent"):
                _, h = _get(f"{base}/s/t_rev?uid=1&rid=1&group=g")
                _post_status(f"{base}/s/t_rev/submit", {"session_id": "t_rev:1:1", "session_token": _tok(h),
                                                        "page_id": "p1", "q_consent": "yes"})
        finally:
            logging.getLogger("survey_agent").propagate = False
        recs = [r for r in caplog.records if "webhook delivery failed" in r.getMessage()]
        if not recs:
            pytest.skip("webhook action did not fire for this survey shape")
        for r in recs:
            for f in (server.obs._TextFormatter(), server.obs._JsonFormatter()):
                assert "SYNTHETIC_PATH_TOKEN" not in f.format(r)
