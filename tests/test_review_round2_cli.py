"""Review round 2, N5: DSAR erasure is a durable job. Before any source record is deleted, the CLI
persists an erasure manifest (subject id + identity fields + scope, upload refs, outbox idempotency keys — never answers);
blobs and queued webhooks are removed and ticked off one by one; the manifest is deleted only when
everything is gone. A failed delete or a crash leaves the manifest, and `dsar --resume` (or any later
`dsar` run) finishes the job. All data here is synthetic and lives under tmp_path."""
import json
import os
import stat

import pytest

from survey_agent import cli
from survey_agent.cli import main
from survey_agent.store import session_webhook_keys
from survey_agent.store.file import FileStore
from survey_agent.store.uploads import LocalUploadStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    surveys, data, ups, obx = (tmp_path / n for n in ("surveys", "data", "uploads", "outbox"))
    surveys.mkdir()
    for k, v in {"SURVEY_STORE": "file", "SURVEY_DATA_DIR": str(data), "SURVEY_DIR": str(surveys),
                 "SURVEY_UPLOAD_STORE": "local", "SURVEY_UPLOAD_DIR": str(ups),
                 "SURVEY_OUTBOX_DIR": str(obx), "SURVEY_AGENT_NO_DOCTOR": "1"}.items():
        monkeypatch.setenv(k, v)
    for k in ("SURVEY_DEFINITION_SOURCE", "SURVEY_ERASURE_DIR"):
        monkeypatch.delenv(k, raising=False)

    class E:
        pass
    e = E()
    e.data, e.ups, e.obx, e.erasure = data, ups, obx, data / "erasure"
    e.store = FileStore(str(data))
    blobs = LocalUploadStore(str(ups))
    e.refs = [blobs.put("s1", "resp-a", "q_photo", "a.png", b"img-a"),
              blobs.put("s1", "resp-a", "q_doc", "b.pdf", b"doc-b")]
    sess, _ = e.store.get_or_create_session("s1", "t1", "u-1", lambda: {
        "session_id": "s1:t1:u-1", "survey_id": "s1", "version": 1, "url_vars": {"uid": "u-1"},
        "answers": {}, "status": "InProgress"})
    sess.update(answers={"q_photo": e.refs[0], "q_free": "SECRET-ANSWER"}, status="Complete",
                fired_webhooks=[0])
    e.store.save_session(sess)
    e.store.record_response("s1", {"id": "resp-a", "survey_id": "s1", "status": "Complete",
                                   "url_variables": {"uid": "u-1"},
                                   "survey_data": {"q_photo": {"id": "q_photo", "answer": e.refs[0]},
                                                   "q_doc": {"id": "q_doc", "answer": e.refs[1]},
                                                   "q_free": {"id": "q_free", "answer": "SECRET-ANSWER"}}})
    e.store.record_response("s1", {"id": "resp-z", "survey_id": "s1", "status": "Complete",
                                   "url_variables": {"uid": "u-2"}})
    (obx / "dead").mkdir(parents=True)
    e.key = session_webhook_keys(sess)[0]
    (obx / "k_1.json").write_text(json.dumps({"id": "k_1", "intent": {"url": "http://h", "idempotency_key": e.key}}))
    (obx / "k_2.json").write_text(json.dumps({"id": "k_2", "intent": {"idempotency_key": "other"}}))
    e.blob = lambda i: ups / e.refs[i]["key"]
    e.manifests = lambda: sorted(p for p in e.erasure.glob("*.json")) if e.erasure.is_dir() else []
    return e


def _assert_records_gone(e):
    assert e.store.get_session_by_id("s1:t1:u-1") is None
    assert [r["id"] for r in e.store.list_responses()] == ["resp-z"]


def _fail_first(monkeypatch, cls, name, exc=RuntimeError("synthetic outage")):
    real, calls = getattr(cls, name), {"n": 0}

    def flaky(self, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc
        return real(self, *a, **kw)
    monkeypatch.setattr(cls, name, flaky)
    return calls


def test_clean_erasure_leaves_no_manifest(env, capsys):
    assert main(["dsar", "u-1", "--delete", "--yes", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["uploads_deleted"], out["outbox_cleared"], out["erasure_job"]) == (2, 1, None)
    _assert_records_gone(env)
    assert not env.blob(0).exists() and not env.blob(1).exists()
    assert not (env.obx / "k_1.json").exists() and (env.obx / "k_2.json").exists()
    assert env.manifests() == []


def test_failed_blob_delete_keeps_manifest_and_resume_finishes(env, monkeypatch, capsys):
    _fail_first(monkeypatch, LocalUploadStore, "delete")
    assert main(["dsar", "u-1", "--delete", "--yes", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["upload_errors"] and out["erasure_job"]
    _assert_records_gone(env)                      # the records are gone …
    assert env.blob(0).exists() and not env.blob(1).exists()   # … one blob left
    [mpath] = env.manifests()
    # the manifest is private and holds only what's needed to finish — never answers or the raw id
    assert stat.S_IMODE(os.stat(mpath).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(env.erasure).st_mode) == 0o700
    raw = mpath.read_text()
    assert "SECRET-ANSWER" not in raw                     # never answers
    m = json.loads(raw)                                     # the subject id is kept (0600) only so an
    assert m["subject"] == "u-1" and m["id_fields"]          # interrupted record erasure can be retried
    m = json.loads(raw)
    assert m["state"] == "records_erased" and m["subject_sha256"] == cli._subject_hash("u-1")
    assert [r["key"] for r in m["pending"]["upload_refs"]] == [env.refs[0]["key"]]
    assert m["pending"]["webhook_keys"] == [] and m["done"]["uploads_deleted"] == 1

    # a plain retry of the subject can't rediscover the blob — the manifest can
    assert main(["dsar", "--resume", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["resumed"][0]["complete"] and res["resumed"][0]["job_id"] == m["job_id"]
    assert not env.blob(0).exists() and env.manifests() == []
    assert main(["dsar", "--resume"]) == 0
    assert "no pending erasure jobs" in capsys.readouterr().out


def test_failure_persists_until_fixed_and_any_dsar_run_retries(env, monkeypatch, capsys):
    real = LocalUploadStore.delete
    monkeypatch.setattr(LocalUploadStore, "delete", lambda self, ref: (_ for _ in ()).throw(OSError("down")))
    assert main(["dsar", "u-1", "--delete", "--yes", "--json"]) == 1
    capsys.readouterr()
    assert main(["dsar", "--resume", "--json"]) == 1          # still failing: non-zero, manifest kept
    res = json.loads(capsys.readouterr().out)["resumed"]
    assert not res[0]["complete"] and res[0]["uploads_pending"] == 2 and "down" in res[0]["errors"][0]
    assert len(env.manifests()) == 1
    # an unrelated lookup also retries pending jobs first, and exits non-zero while one is stuck
    assert main(["dsar", "someone-else", "--json"]) == 1
    capsys.readouterr()
    monkeypatch.setattr(LocalUploadStore, "delete", real)
    assert main(["dsar", "someone-else", "--json"]) == 0      # now the retry succeeds
    out = json.loads(capsys.readouterr().out)
    assert out["resumed"][0]["complete"] and out["resumed"][0]["uploads_deleted"] == 2
    assert env.manifests() == [] and not env.blob(0).exists() and not env.blob(1).exists()


def test_crash_after_record_deletion_is_resumable(env, monkeypatch):
    # the process dies right after the store erased the records, before any blob/outbox removal
    def crash(refs):
        raise KeyboardInterrupt("simulated crash")
    monkeypatch.setattr(cli, "_dsar_delete_uploads", crash)
    with pytest.raises(KeyboardInterrupt):
        main(["dsar", "u-1", "--delete", "--yes", "--json"])
    _assert_records_gone(env)
    assert env.blob(0).exists() and env.blob(1).exists() and (env.obx / "k_1.json").exists()
    [mpath] = env.manifests()
    m = json.loads(mpath.read_text())
    assert m["state"] == "records_erased" and len(m["pending"]["upload_refs"]) == 2
    assert m["pending"]["webhook_keys"] == [env.key]

    monkeypatch.undo()
    for k, v in {"SURVEY_STORE": "file", "SURVEY_DATA_DIR": str(env.data), "SURVEY_UPLOAD_STORE": "local",
                 "SURVEY_UPLOAD_DIR": str(env.ups), "SURVEY_OUTBOX_DIR": str(env.obx)}.items():
        monkeypatch.setenv(k, v)
    assert main(["dsar", "--resume", "--json"]) == 0
    assert not env.blob(0).exists() and not env.blob(1).exists()
    assert not (env.obx / "k_1.json").exists() and (env.obx / "k_2.json").exists()
    assert env.manifests() == []


def test_crash_during_record_erasure_leaves_manifest_written_first(env, monkeypatch, capsys):
    # the manifest must exist BEFORE the store deletes anything
    seen = {}

    def boom(self, *a, **kw):
        seen["manifests"] = env.manifests()
        raise KeyboardInterrupt("simulated crash mid-erase")
    monkeypatch.setattr(FileStore, "erase_respondent", boom)
    with pytest.raises(KeyboardInterrupt):
        main(["dsar", "u-1", "--delete", "--yes", "--json"])
    assert len(seen["manifests"]) == 1
    m = json.loads(seen["manifests"][0].read_text())
    assert m["state"] == "erasing_records" and len(m["pending"]["upload_refs"]) == 2
    monkeypatch.undo()
    for k, v in {"SURVEY_STORE": "file", "SURVEY_DATA_DIR": str(env.data), "SURVEY_UPLOAD_STORE": "local",
                 "SURVEY_UPLOAD_DIR": str(env.ups), "SURVEY_OUTBOX_DIR": str(env.obx)}.items():
        monkeypatch.setenv(k, v)
    # resume re-runs the record erasure itself, and only then reports the job complete
    assert main(["dsar", "--resume", "--json"]) == 0
    res = json.loads(capsys.readouterr().out)["resumed"][0]
    assert res["complete"] and res["records_confirmed"]
    _assert_records_gone(env)
    assert env.manifests() == []


def test_resume_never_reports_complete_while_records_remain(env, monkeypatch, capsys):
    """Round-3 review: a job interrupted before the store confirmed the records are erased must stay
    pending (non-zero exit, manifest kept) until a retry of the record erasure succeeds."""
    def boom(self, *a, **kw):
        raise KeyboardInterrupt("simulated crash before records are erased")
    monkeypatch.setattr(FileStore, "erase_respondent", boom)
    with pytest.raises(KeyboardInterrupt):
        main(["dsar", "u-1", "--delete", "--yes", "--json"])
    # the store keeps failing → resume must not succeed, must keep the manifest, records still there
    def fail(self, *a, **kw):
        raise OSError("synthetic store outage")
    monkeypatch.setattr(FileStore, "erase_respondent", fail)
    assert main(["dsar", "--resume", "--json"]) == 1
    res = json.loads(capsys.readouterr().out)["resumed"][0]
    assert not res["complete"] and not res["records_confirmed"]
    assert len(env.manifests()) == 1
    # store recovers → resume finishes everything and only then removes the manifest
    monkeypatch.undo()
    for k, v in {"SURVEY_STORE": "file", "SURVEY_DATA_DIR": str(env.data), "SURVEY_UPLOAD_STORE": "local",
                 "SURVEY_UPLOAD_DIR": str(env.ups), "SURVEY_OUTBOX_DIR": str(env.obx)}.items():
        monkeypatch.setenv(k, v)
    assert main(["dsar", "--resume", "--json"]) == 0
    _assert_records_gone(env)
    assert env.manifests() == []


def test_record_only_erasure_is_resumable(env, monkeypatch, capsys):
    """A subject with no uploads and no queued deliveries still gets a manifest before deletion."""
    from survey_agent.store.file import FileStore as FS
    st = FS(str(env.data))
    st.record_response("s", {"id": "plain-1", "status": "Complete", "url_variables": {"uid": "u-plain"}})
    def boom(self, *a, **kw):
        raise KeyboardInterrupt("crash")
    monkeypatch.setattr(FileStore, "erase_respondent", boom)
    with pytest.raises(KeyboardInterrupt):
        main(["dsar", "u-plain", "--delete", "--yes", "--json"])
    assert len(env.manifests()) == 1
    monkeypatch.undo()
    for k, v in {"SURVEY_STORE": "file", "SURVEY_DATA_DIR": str(env.data), "SURVEY_UPLOAD_STORE": "local",
                 "SURVEY_UPLOAD_DIR": str(env.ups), "SURVEY_OUTBOX_DIR": str(env.obx)}.items():
        monkeypatch.setenv(k, v)
    assert main(["dsar", "--resume", "--json"]) == 0
    assert not [r for r in st.list_responses() if (r.get("url_variables") or {}).get("uid") == "u-plain"]
    assert env.manifests() == []


def test_failed_outbox_clear_keeps_keys_for_retry(env, monkeypatch, capsys):
    from survey_agent.integrations.outbox import Outbox
    _fail_first(monkeypatch, Outbox, "remove_by_idempotency_key", PermissionError("synthetic"))
    assert main(["dsar", "u-1", "--delete", "--yes", "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["uploads_deleted"] == 2 and out["outbox_cleared"] == 0 and out["erasure_job"]
    [mpath] = env.manifests()
    m = json.loads(mpath.read_text())
    assert m["pending"]["upload_refs"] == [] and m["pending"]["webhook_keys"] == [env.key]
    assert (env.obx / "k_1.json").exists()
    assert main(["dsar", "--resume", "--json"]) == 0
    assert not (env.obx / "k_1.json").exists() and (env.obx / "k_2.json").exists()
    assert env.manifests() == []


def test_dsar_requires_uid_or_resume(env, capsys):
    assert main(["dsar", "--json"]) == 2
    assert "error" in json.loads(capsys.readouterr().out)


def test_corrupt_manifest_is_reported_not_skipped(env, capsys):
    env.erasure.mkdir(parents=True)
    (env.erasure / "bad.json").write_text("{not json")
    assert main(["dsar", "--resume", "--json"]) == 1
    res = json.loads(capsys.readouterr().out)["resumed"]
    assert res[0]["job_id"] == "bad" and not res[0]["complete"]
