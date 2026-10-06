"""Release-review fixes on the storage seams:

R4  file definitions keep immutable per-version snapshots (get_definition honours `version`).
R5  respondent erasure covers responses AND sessions (partial, retakes, every survey), custom identity
    vars, upload blobs and queued webhook deliveries; the DSAR CLI drives it.
R6  reserve_quotas: all-or-nothing, atomic capacity reservation on every response store.
"""
import json
import os
import threading

import pytest

from survey_agent.cli import main
from survey_agent.definitions.file import FileDefinitionStore
from survey_agent.store.file import FileStore
from survey_agent.store.memory import MemoryStore
from survey_agent.store.sqlite import SQLiteStore


def _spec(sid="s1", version=1, title="Original", respondent=None):
    s = {"id": sid, "version": version, "title": title, "settings": {"url_variables": []},
         "pages": [{"id": "p1", "questions": [{"id": "q1", "type": "text"}]}],
         "actions": [{"type": "complete", "redirect": {"kind": "default"}}]}
    if respondent:
        s["settings"]["identity"] = {"respondent": respondent}
    return {"survey": s}


# ------------------------------------------------------------------ R4: file definition versions

def test_file_definitions_keep_old_versions(tmp_path):
    d = FileDefinitionStore(dir=str(tmp_path))
    assert d.put_definition(_spec(version=1, title="Original")) == 1
    assert d.put_definition(_spec(version=2, title="Changed")) == 2
    assert d.get_definition("s1", version=1)["survey"]["title"] == "Original"
    assert d.get_definition("s1", version=2)["survey"]["title"] == "Changed"
    assert d.get_definition("s1")["survey"]["version"] == 2
    assert d.get_registry("s1")["live_version"] == 2
    assert [v for v, _, _ in d.iter_versions("s1")] == [1, 2]
    # survives a "restart" (a fresh store instance reads the snapshots off disk)
    again = FileDefinitionStore(dir=str(tmp_path))
    assert again.get_definition("s1", version=1)["survey"]["title"] == "Original"
    assert again.get_definition("s1", version=3) is None


def test_file_definitions_never_overwrite_a_version(tmp_path):
    d = FileDefinitionStore(dir=str(tmp_path))
    d.put_definition(_spec(version=1, title="A"))
    assert d.put_definition(_spec(version=1, title="A")) == 1          # identical → no-op
    assert d.put_definition(_spec(version=1, title="B")) == 2          # reused number → bumped
    cur = json.load(open(tmp_path / "s1.survey.json"))
    assert cur["survey"]["version"] == 2 and cur["survey"]["title"] == "B"
    assert d.get_definition("s1", version=1)["survey"]["title"] == "A"
    assert d.put_definition(_spec(version=7, title="C")) == 7          # explicit bump honoured
    assert [v for v, _, _ in d.iter_versions("s1")] == [1, 2, 7]
    with pytest.raises(FileExistsError):
        d._snapshot("s1", 1, _spec(title="tamper"))


def test_file_definitions_hand_edit_without_bump_gets_a_new_version(tmp_path):
    d = FileDefinitionStore(dir=str(tmp_path))
    d.put_definition(_spec(version=1, title="A"))
    assert d.get_registry("s1")["live_version"] == 1
    # an author edits the file in place (git pull / editor) and forgets to bump `version`
    with open(tmp_path / "s1.survey.json", "w") as f:
        json.dump(_spec(version=1, title="Edited"), f)
    os.utime(tmp_path / "s1.survey.json", ns=(1, 1))                   # defeat the mtime cache
    assert d.get_registry("s1")["live_version"] == 2
    assert d.get_definition("s1", version=2)["survey"]["title"] == "Edited"
    assert d.get_definition("s1", version=1)["survey"]["title"] == "A"  # in-flight v1 unchanged


def test_file_definitions_first_read_snapshots_unpublished_file(tmp_path):
    with open(tmp_path / "s1.survey.json", "w") as f:
        json.dump(_spec(version=3, title="Committed"), f)
    d = FileDefinitionStore(dir=str(tmp_path))
    assert d.get_definition("s1", version=3)["survey"]["title"] == "Committed"
    assert (tmp_path / ".versions" / "s1" / "v3.survey.json").exists()


def test_versions_dir_is_not_a_survey(tmp_path):
    from survey_agent import registry
    d = FileDefinitionStore(dir=str(tmp_path))
    d.put_definition(_spec(version=1))
    d.put_definition(_spec(version=2, title="v2"))
    assert [e["id"] for e in d.list_surveys()] == ["s1"]
    assert [e["file"] for e in registry.catalog(str(tmp_path))] == ["s1.survey.json"]
    d.delete_definition("s1")
    assert not (tmp_path / ".versions" / "s1").exists()
    assert d.get_definition("s1") is None and d.get_definition("s1", version=1) is None


def test_mongo_definitions_honour_version():
    mongomock = pytest.importorskip("mongomock")
    from survey_agent.definitions.mongo import MongoDefinitionStore
    db = mongomock.MongoClient()["t"]
    m = MongoDefinitionStore(definitions=db["defs"], registry=db["reg"])
    m.put_definition(_spec(title="Original"))
    m.put_definition(_spec(title="Changed"))
    assert m.get_definition("s1", version=1)["survey"]["title"] == "Original"
    assert m.get_definition("s1", version=2)["survey"]["title"] == "Changed"


def test_http_definitions_pass_version(monkeypatch):
    from survey_agent.definitions.http import HTTPDefinitionStore
    h = HTTPDefinitionStore(base="http://x")
    seen = []
    monkeypatch.setattr(h, "_req", lambda method, path, **k: seen.append(path) or {})
    h.get_definition("s1", version=1)
    assert seen == ["/v1/definitions/s1?version=1"]


# ------------------------------------------------------------------ store fixtures

def _mongo_store():
    pytest.importorskip("pymongo")
    mongomock = pytest.importorskip("mongomock")
    from survey_agent.store.mongo import MongoStore
    return MongoStore(client=mongomock.MongoClient(), db="t")


@pytest.fixture(params=["memory", "file", "sqlite", "mongo"])
def store(request, tmp_path):
    kind = request.param
    if kind == "memory":
        return MemoryStore()
    if kind == "file":
        return FileStore(str(tmp_path / "data"))
    if kind == "sqlite":
        return SQLiteStore(str(tmp_path / "s.db"))
    return _mongo_store()


REF = {"backend": "local", "key": "s1/r1/q_photo/a.png", "filename": "a.png", "size": 3}


def _session(store, survey_id, rid, url_vars, answers=None, status="InProgress", fired=None):
    uid = url_vars.get("uid") or url_vars.get("pid") or "x"
    sess, _ = store.get_or_create_session(survey_id, rid, uid, lambda: {
        "session_id": f"{survey_id}:{rid}:{uid}", "survey_id": survey_id, "version": 1,
        "url_vars": dict(url_vars), "answers": {}, "status": "InProgress"})
    sess["answers"] = answers or {}
    sess["status"] = status
    if fired:
        sess["fired_webhooks"] = fired
    store.save_session(sess)
    return sess


def _response(store, survey_id, rid, url_vars, answers=None):
    store.record_response(survey_id, {
        "id": rid, "survey_id": survey_id, "status": "Complete", "url_variables": dict(url_vars),
        "survey_data": {k: {"id": k, "answer": v} for k, v in (answers or {}).items()}})


# ------------------------------------------------------------------ R5: respondent erasure

def test_erase_respondent_covers_responses_sessions_retakes_and_partials(store):
    # u1: a completed survey with an upload, retaken twice (2 responses, 1 session doc) ...
    s1 = _session(store, "s1", "r1", {"uid": "u1", "rid": "r1"}, {"q_photo": REF}, "Complete", fired=[0])
    _response(store, "s1", "resp-a", {"uid": "u1", "rid": "r1"}, {"q_photo": REF})
    _response(store, "s1", "resp-b", {"uid": "u1", "rid": "r1"}, {"q1": "again"})
    # ... a partial session on another survey (never produced a response) ...
    _session(store, "s2", "r9", {"uid": "u1", "rid": "r9"}, {"q1": "half"})
    # ... and someone else, who must be untouched
    other = _session(store, "s1", "r2", {"uid": "u2", "rid": "r2"}, {"q1": "keep"}, "Complete")
    _response(store, "s1", "resp-c", {"uid": "u2", "rid": "r2"}, {"q1": "keep"})

    found = store.find_respondent("u1")
    assert len(found["responses"]) == 2 and len(found["sessions"]) == 2

    res = store.erase_respondent("u1")
    assert res["responses"] == 2 and res["sessions"] == 2
    assert [r["key"] for r in res["upload_refs"]] == [REF["key"]]          # de-duplicated
    assert len(res["webhook_keys"]) == 1
    assert store.get_session_by_id(s1["session_id"]) is None
    assert store.find_respondent("u1") == {"responses": [], "sessions": []}
    assert store.get_session_by_id(other["session_id"]) is not None
    assert [r["id"] for r in store.list_responses()] == ["resp-c"]


def test_erase_respondent_custom_identity_var(store):
    _session(store, "s1", "r1", {"pid": "p-7", "tid": "r1"}, {"q1": "x"})
    _response(store, "s1", "resp-a", {"pid": "p-7", "tid": "r1"})
    _response(store, "s1", "resp-b", {"uid": "p-7"})                       # same person, default var
    # hard-coded uid lookup misses the pid response; the identity-aware one finds both
    assert [r["id"] for r in store.list_responses(uid="p-7")] == ["resp-b"]
    assert {r["id"] for r in store.list_responses(uid="p-7", id_fields=["uid", "pid"])} == {"resp-a", "resp-b"}
    res = store.erase_respondent("p-7", id_fields=("uid", "pid"))
    assert res["responses"] == 2 and res["sessions"] == 1
    assert store.list_responses() == []


def test_erase_respondent_survey_scope(store):
    _response(store, "s1", "a", {"uid": "u1"})
    _response(store, "s2", "b", {"uid": "u1"})
    assert store.erase_respondent("u1", survey_id="s1")["responses"] == 1
    assert [r["id"] for r in store.list_responses()] == ["b"]


def test_delete_responses_wrapper_also_drops_sessions(store):
    sess = _session(store, "s1", "r1", {"uid": "u1"}, {"q1": "private"})
    _response(store, "s1", "a", {"uid": "u1"})
    assert store.delete_responses(uid="u1") == 1
    assert store.get_session_by_id(sess["session_id"]) is None


def test_http_store_refuses_erasure():
    from survey_agent.store.http import HTTPStore
    h = HTTPStore(base="http://x")
    for call in (lambda: h.erase_respondent("u"), lambda: h.find_respondent("u"),
                 lambda: h.reserve_quotas("s", 1, {"q": 1}),
                 lambda: h.list_responses("s", uid="u", id_fields=["pid"])):
        with pytest.raises(NotImplementedError):
            call()


def test_dsar_cli_erases_everything_for_a_custom_identity(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    surveys, data, ups, obx = (tmp_path / n for n in ("surveys", "data", "uploads", "outbox"))
    surveys.mkdir()
    for k, v in {"SURVEY_STORE": "file", "SURVEY_DATA_DIR": str(data), "SURVEY_DIR": str(surveys),
                 "SURVEY_UPLOAD_STORE": "local", "SURVEY_UPLOAD_DIR": str(ups),
                 "SURVEY_OUTBOX_DIR": str(obx), "SURVEY_AGENT_NO_DOCTOR": "1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SURVEY_DEFINITION_SOURCE", raising=False)
    FileDefinitionStore(dir=str(surveys)).put_definition(_spec("panel", respondent="pid"))

    from survey_agent.store.uploads import LocalUploadStore
    ref = LocalUploadStore(str(ups)).put("panel", "resp-a", "q_photo", "a.png", b"img")
    st = FileStore(str(data))
    sess = _session(st, "panel", "t1", {"pid": "p-7", "tid": "t1"}, {"q_photo": ref}, "Complete", fired=[0])
    _response(st, "panel", "resp-a", {"pid": "p-7", "tid": "t1"}, {"q_photo": ref})
    _response(st, "panel", "resp-z", {"pid": "p-8", "tid": "t2"})
    # a queued webhook delivery this session fired (it carries piped respondent data)
    from survey_agent.store import session_webhook_keys
    (obx / "dead").mkdir(parents=True)
    key = session_webhook_keys(sess)[0]
    (obx / "k_1.json").write_text(json.dumps({"id": "k_1", "intent": {"url": "http://h", "payload": {"a": "p-7"},
                                                                       "idempotency_key": key}}))
    (obx / "k_2.json").write_text(json.dumps({"id": "k_2", "intent": {"idempotency_key": "other"}}))

    assert main(["dsar", "p-7", "--json", "--full"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert "pid" in listed["id_fields"] and listed["count"] == 1 and listed["session_count"] == 1
    assert listed["records"]["responses"][0]["id"] == "resp-a"

    assert main(["dsar", "p-7", "--delete", "--yes", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["deleted"], out["sessions_deleted"], out["uploads_deleted"], out["outbox_cleared"]) == (1, 1, 1, 1)
    assert "passbacks" in out["not_erased"] and "backup" in out["not_erased"]
    assert not (ups / ref["key"]).exists()
    assert not (obx / "k_1.json").exists() and (obx / "k_2.json").exists()
    assert st.get_session_by_id(sess["session_id"]) is None
    assert [r["id"] for r in st.list_responses()] == ["resp-z"]


def test_dsar_cli_honours_runtime_default_identity(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SURVEY_STORE", "sqlite")
    monkeypatch.setenv("SURVEY_SQLITE_PATH", str(tmp_path / "d.db"))
    monkeypatch.setenv("SURVEY_DIR", str(tmp_path / "none"))
    monkeypatch.setenv("SURVEY_RESPONDENT_VAR", "member")
    monkeypatch.setenv("SURVEY_AGENT_NO_DOCTOR", "1")
    _response(SQLiteStore(str(tmp_path / "d.db")), "s1", "a", {"member": "m1"})
    assert main(["dsar", "m1", "--delete", "--yes", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["deleted"] == 1 and "member" in out["id_fields"]


# ------------------------------------------------------------------ R6: quota reservation

def test_reserve_quotas_all_or_nothing(store):
    assert store.reserve_quotas("s1", 1, {}) is True
    assert store.reserve_quotas("s1", 1, {"total": 2, "women": 1, "open": None}) is True
    assert store.quota_counts("s1", 1) == {"total": 1, "women": 1, "open": 1}
    # women is full → nothing moves, not even the unlimited / non-full buckets
    assert store.reserve_quotas("s1", 1, {"total": 2, "women": 1, "open": None}) is False
    assert store.quota_counts("s1", 1) == {"total": 1, "women": 1, "open": 1}
    assert store.reserve_quotas("s1", 1, {"total": 2, "men": 1}) is True
    assert store.reserve_quotas("s1", 1, {"total": 2}) is False                     # total now full
    assert store.quota_counts("s1", 1) == {"total": 2, "women": 1, "open": 1, "men": 1}
    assert store.reserve_quotas("s1", 2, {"total": 2}) is True                      # per version
    assert store.reserve_quotas("s1", 1, {"zero": 0}) is False
    assert store.incr_quota("s1", 1, "open") == 2                                   # still works


@pytest.mark.parametrize("kind", ["memory", "file", "sqlite"])
def test_reserve_quotas_concurrent_last_slot(kind, tmp_path):
    def make():
        if kind == "memory":
            return MemoryStore()
        if kind == "file":
            return FileStore(str(tmp_path / "data"))
        return SQLiteStore(str(tmp_path / "s.db"))
    shared = make()
    # file/sqlite: every thread gets its own store instance (separate locks/connections), so only
    # the OS file lock / SQLite transaction can serialize them — the cross-worker case.
    stores = [shared if kind == "memory" else make() for _ in range(16)]
    results, barrier = [], threading.Barrier(len(stores))

    def go(s):
        barrier.wait()
        results.append(s.reserve_quotas("s1", 1, {"total": 1, "seg": None}))

    threads = [threading.Thread(target=go, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and results.count(False) == len(stores) - 1
    assert shared.quota_counts("s1", 1) == {"total": 1, "seg": 1}


def test_mongo_reserve_sequential_rolls_back_partial():
    m = _mongo_store()
    assert m.reserve_quotas("s1", 1, {"a": 5, "b": 1}) is True
    # 'a' would increment first, then 'b' is full → 'a' must be compensated back
    assert m.reserve_quotas("s1", 1, {"a": 5, "b": 1}) is False
    assert m.quota_counts("s1", 1) == {"a": 1, "b": 1}
    assert m._txn_ok is False                         # mongomock has no sessions → sequential path


@pytest.mark.parametrize("fields,forwarded", [
    ({"status": "live"}, True), ({"status": "paused"}, True), ({"live": {"url": "u"}}, True),
    ({"live_version": 2}, True), ({"note": "hi"}, False), ({"validated": True}, False)])
def test_http_set_registry_forwards_launch_approval(monkeypatch, fields, forwarded):
    from survey_agent import config
    from survey_agent.definitions.http import HTTPDefinitionStore
    monkeypatch.setattr(config, "_APPROVED", "tok-123")
    h = HTTPDefinitionStore(base="http://x")
    seen = {}
    monkeypatch.setattr(h, "_req", lambda method, path, body=None, extra_headers=None:
                        seen.update(headers=extra_headers) or {})
    h.set_registry("s1", **fields)
    assert (seen["headers"] == {"X-Survey-Launch-Approval": "tok-123"}) is forwarded
    monkeypatch.setattr(config, "_APPROVED", None)                 # nothing approved → never sent
    h.set_registry("s1", **fields)
    assert seen["headers"] is None


def test_launch_writes_the_version_snapshot_next_to_the_survey(tmp_path, monkeypatch):
    """A git-based deploy must carry the snapshot of the launched version, or a redeploy to ephemeral
    disk would strand respondents mid-survey on it."""
    import json
    import shutil
    from survey_agent import cli
    src = os.path.join(os.path.dirname(__file__), "..", "surveys", "streaming-usage.survey.json")
    d = tmp_path / "surveys"
    d.mkdir()
    shutil.copy(src, d / "streaming-usage.survey.json")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SURVEY_LAUNCH_APPROVAL_TOKEN", raising=False)
    monkeypatch.setenv("SURVEY_OPERATOR_ACK", "1")
    cli.main(["launch", str(d / "streaming-usage.survey.json"), "--base", "https://surveys.example.com"])
    snap = d / ".versions" / "streaming-usage" / "v1.survey.json"
    assert snap.exists()
    assert json.loads(snap.read_text())["survey"]["id"] == "streaming-usage"
