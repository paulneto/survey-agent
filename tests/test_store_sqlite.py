"""SQLite store: contract + the concurrency guarantee that's the reason it exists."""
import os
import threading

import pytest

from survey_agent import store as store_pkg
from survey_agent.store.sqlite import SQLiteStore


@pytest.fixture()
def db(tmp_path):
    return SQLiteStore(str(tmp_path / "survey.db"))


def test_factory_wires_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("SURVEY_SQLITE_PATH", str(tmp_path / "x.db"))
    s = store_pkg.get_store("sqlite")
    assert isinstance(s, SQLiteStore)
    assert "sqlite" in store_pkg.BUILTINS


def test_sessions_roundtrip_and_idempotent_create(db):
    made = {"n": 0}

    def factory():
        made["n"] += 1
        return {"session_id": "s1:r1:u1", "status": "InProgress", "page": 0}

    sess, created = db.get_or_create_session("s1", "r1", "u1", factory)
    assert created and sess["page"] == 0
    again, created2 = db.get_or_create_session("s1", "r1", "u1", factory)
    assert not created2 and again["session_id"] == "s1:r1:u1"
    assert made["n"] == 1  # factory not called twice

    sess["page"] = 3
    db.save_session(sess)
    assert db.get_session_by_id("s1:r1:u1")["page"] == 3
    assert db.get_session_by_id("nope") is None


def test_responses_record_list_count(db):
    db.record_response("s1", {"id": "a", "status": "Complete", "url_variables": {"uid": "u1"}})
    db.record_response("s1", {"id": "b", "status": "Disqualified", "url_variables": {"uid": "u2"}})
    db.record_response("s2", {"id": "c", "status": "Complete", "url_variables": {"uid": "u1"}})

    assert len(db.list_responses("s1")) == 2
    assert [r["id"] for r in db.list_responses("s1", uid="u1")] == ["a"]
    assert len(db.list_responses()) == 3  # all surveys

    counts = db.count_responses_by_survey()
    assert counts["s1"] == {"complete": 1, "disqualified": 1, "total": 2}
    assert counts["s2"]["complete"] == 1


def test_quota_increment_is_atomic_under_threads(db):
    """The whole point of sqlite over file: concurrent increments don't lose updates."""
    n = 200
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        for _ in range(n // 8):
            db.incr_quota("s1", 1, "q_total")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert db.quota_counts("s1", 1)["q_total"] == n


def test_ping_ok(db):
    db.ping()  # should not raise
