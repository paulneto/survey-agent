"""Regression tests for the second release review's definition findings (N1, N4).

N1 — saving a definition must never move what is live. `live_version` is the approved pointer; only an
     approval-gated mark-live moves it. Checked on the gateway PUT path, on the file / mongo / http
     backends, across a restart, and for the git-deploy (hand-edited file) flow.
N4 — the gateway validates definitions before storing them, and the file backend independently refuses
     ids that are not `^[A-Za-z0-9_-]+$` and any path that resolves outside its root.

Temp dirs, in-memory/mock stores and synthetic surveys only.
"""
import importlib
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from survey_agent import registry
from survey_agent.definitions.base import InvalidSurveyId
from survey_agent.definitions.file import FileDefinitionStore


def _spec(sid="rv", version=1, title="Original question"):
    return {"survey": {"id": sid, "version": version, "title": "Review",
                       "settings": {"url_variables": ["uid"]},
                       "pages": [{"id": "p1", "questions": [{"id": "q1", "type": "text", "title": title}]}],
                       "actions": [{"type": "complete", "redirect": {"kind": "default"}}]}}


def _q1(spec):
    return spec["survey"]["pages"][0]["questions"][0]["title"]


BAD_IDS = ["../x", "../outside", "/abs", "/etc/passwd", "a/b", "a\\b", "..", ".", "", "a.b", " ", None]


# =============================================================================== gateway fixtures

_ENV = ("SURVEY_GOOGLE_CLIENT_ID", "SURVEY_GOOGLE_CLIENT_SECRET", "SURVEY_SESSION_JWT_KEY",
        "SURVEY_API_TOKEN", "SURVEY_LAUNCH_APPROVAL_TOKEN")


def _gateway(monkeypatch, sdir):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    import survey_agent.api.app as appmod
    from survey_agent.api.auth import apikeys
    from survey_agent.api.auth import config as authcfg
    from survey_agent.api.auth.deps import get_key_store
    for v in _ENV:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("SURVEY_DEFINITION_SOURCE", "file")
    monkeypatch.setenv("SURVEY_DIR", str(sdir))
    monkeypatch.setenv("SURVEY_STORE", "file")
    monkeypatch.setenv("SURVEY_DATA_DIR", str(sdir.parent / "data"))
    monkeypatch.setenv("SURVEY_API_TOKEN", "api-only-token")
    monkeypatch.setenv("SURVEY_LAUNCH_APPROVAL_TOKEN", "human-approval")
    importlib.reload(authcfg)
    app = importlib.reload(appmod).app
    store = apikeys.InMemoryKeyStore()
    app.dependency_overrides[get_key_store] = lambda: store
    c = TestClient(app)
    c.h = {"Authorization": "Bearer api-only-token"}
    c.ok = {**c.h, "X-Survey-Launch-Approval": "human-approval"}
    return c


@pytest.fixture(autouse=True)
def _reset_gateway():
    yield
    for v in _ENV:
        os.environ.pop(v, None)
    try:
        import survey_agent.api.app as appmod
        from survey_agent.api.auth import config as authcfg
        importlib.reload(authcfg)
        importlib.reload(appmod)
    except Exception:
        pass


# =============================================================================== N1: gateway

def test_n1_reviewer_reproduction_put_does_not_move_live(monkeypatch, tmp_path):
    sdir = tmp_path / "definitions"
    # seed a synthetic v1 that is live and pinned, directly in the temp store
    seed = FileDefinitionStore(dir=str(sdir))
    assert seed.put_definition(_spec()) == 1
    registry.record("rv", sdir=str(sdir), status="live", live={"at": "seed"}, live_version=1)

    c = _gateway(monkeypatch, sdir)
    r = c.put("/v1/definitions", headers=c.h, json=_spec(title="UNAPPROVED replacement"))
    assert r.status_code == 200 and r.json() == {"version": 2}       # stored as a new version…
    reg = c.get("/v1/registry/rv", headers=c.h).json()
    assert reg["status"] == "live" and reg["live_version"] == 1     # …but the live pointer did not move
    assert reg["latest_version"] == 2
    assert _q1(c.get("/v1/definitions/rv", headers=c.h).json()) == "Original question"
    assert _q1(c.get("/v1/definitions/rv?version=2", headers=c.h).json()) == "UNAPPROVED replacement"

    # moving the pointer without approval is refused, every way it can be expressed
    for body in ({"live_version": 2}, {"status": "live"}, {"live": {"at": "x"}},
                 {"status": "live", "approve": "wrong"}):
        assert c.post("/v1/registry/rv", headers=c.h, json=body).status_code == 403, body
    assert c.get("/v1/registry/rv", headers=c.h).json()["live_version"] == 1

    # an unknown version can't be pinned even with approval
    assert c.post("/v1/registry/rv", headers=c.ok, json={"live_version": 9}).status_code == 400

    # the approved mark-live moves it (no explicit version → the newest authored version)
    r = c.post("/v1/registry/rv", headers=c.ok, json={"status": "live"})
    assert r.status_code == 200 and r.json()["live_version"] == 2
    assert _q1(c.get("/v1/definitions/rv", headers=c.h).json()) == "UNAPPROVED replacement"


def test_n1_taking_out_of_live_then_editing_still_serves_the_approved_pin(monkeypatch, tmp_path):
    sdir = tmp_path / "definitions"
    c = _gateway(monkeypatch, sdir)
    assert c.put("/v1/definitions", headers=c.h, json=_spec()).status_code == 200
    assert c.post("/v1/registry/rv", headers=c.ok, json={"status": "live"}).json()["live_version"] == 1
    # un-gated: drop it to draft, then replace the content
    assert c.post("/v1/registry/rv", headers=c.h, json={"status": "draft"}).status_code == 200
    assert c.put("/v1/definitions", headers=c.h, json=_spec(title="sneaky")).json() == {"version": 2}
    reg = c.get("/v1/registry/rv", headers=c.h).json()
    assert reg["live_version"] == 1 and reg["latest_version"] == 2
    assert _q1(c.get("/v1/definitions/rv", headers=c.h).json()) == "Original question"


# =============================================================================== N1: file backend

def test_n1_file_put_never_moves_pin_and_survives_restart(tmp_path):
    d = FileDefinitionStore(dir=str(tmp_path))
    d.put_definition(_spec())
    d.set_registry("rv", status="live")                    # caller already passed the gate
    assert d.get_registry("rv")["live_version"] == 1
    assert d.put_definition(_spec(title="edit")) == 2
    reg = d.get_registry("rv")
    assert (reg["live_version"], reg["latest_version"]) == (1, 2)
    assert _q1(d.get_definition("rv")) == "Original question"
    again = FileDefinitionStore(dir=str(tmp_path))         # "restart"
    reg = again.get_registry("rv")
    assert (reg["live_version"], reg["latest_version"]) == (1, 2)
    assert _q1(again.get_definition("rv")) == "Original question"
    assert json.load(open(tmp_path / ".registry.json"))["surveys"]["rv"]["live_version"] == 1


def test_n1_file_git_deploy_flow(tmp_path):
    """Author edits the file and bumps the version: nothing changes for respondents until an approved
    publish/launch; then the pointer moves to the launched version."""
    d = FileDefinitionStore(dir=str(tmp_path))
    d.put_definition(_spec())
    registry.set_status("rv", status="live", sdir=str(tmp_path))    # what `launch` does after its gate
    assert d.get_registry("rv")["live_version"] == 1
    with open(tmp_path / "rv.survey.json", "w") as f:                 # plain edit + bump, no approval
        json.dump(_spec(version=2, title="v2 edit"), f)
    os.utime(tmp_path / "rv.survey.json", ns=(1, 1))
    fresh = FileDefinitionStore(dir=str(tmp_path))
    reg = fresh.get_registry("rv")
    assert (reg["live_version"], reg["latest_version"]) == (1, 2)
    assert _q1(fresh.get_definition("rv")) == "Original question"
    # approved launch → moves to the newest authored version
    registry.set_status("rv", status="live", sdir=str(tmp_path))
    assert fresh.get_registry("rv")["live_version"] == 2
    assert _q1(fresh.get_definition("rv")) == "v2 edit"
    # `publish` passes the stored version explicitly
    d.set_registry("rv", status="live", live_version=1)
    assert _q1(FileDefinitionStore(dir=str(tmp_path)).get_definition("rv")) == "Original question"


def test_n1_file_legacy_unpinned_live_is_pinned_to_what_it_served(tmp_path):
    d = FileDefinitionStore(dir=str(tmp_path))
    d.put_definition(_spec())
    registry.record("rv", sdir=str(tmp_path), status="live")          # pre-pointer registry: no live_version
    assert d.get_registry("rv")["live_version"] == 1
    assert json.load(open(tmp_path / ".registry.json"))["surveys"]["rv"]["live_version"] == 1
    d.put_definition(_spec(title="later"))
    assert d.get_registry("rv")["live_version"] == 1
    assert _q1(d.get_definition("rv")) == "Original question"


def test_n1_file_never_live_draft_reports_newest(tmp_path):
    d = FileDefinitionStore(dir=str(tmp_path))
    d.put_definition(_spec())
    d.put_definition(_spec(title="draft 2"))
    reg = d.get_registry("rv")
    assert reg["live_version"] == reg["latest_version"] == 2


# =============================================================================== N1: mongo backend

def _mongo_store(db=None):
    mongomock = pytest.importorskip("mongomock")
    from survey_agent.definitions.mongo import MongoDefinitionStore
    db = db if db is not None else mongomock.MongoClient()["n1"]
    return MongoDefinitionStore(definitions=db["survey_definitions"], registry=db["survey_registry"]), db


def test_n1_mongo_put_never_moves_pin_and_survives_restart():
    m, db = _mongo_store()
    m.put_definition(_spec())
    m.set_registry("rv", status="live")                    # no explicit version → pins the newest (1)
    assert m.get_registry("rv")["live_version"] == 1
    assert m.put_definition(_spec(title="edit")) == 2
    reg = m.get_registry("rv")
    assert (reg["live_version"], reg["latest_version"]) == (1, 2)
    assert _q1(m.get_definition("rv")) == "Original question"
    again, _ = _mongo_store(db)                            # "restart": new store, same database
    assert again.get_registry("rv")["live_version"] == 1
    assert _q1(again.get_definition("rv")) == "Original question"
    assert "latest_version" not in db["survey_registry"].find_one({"_id": "rv"})   # derived, not stored
    again.set_registry("rv", status="live")                # an approved re-launch moves it
    assert _q1(again.get_definition("rv")) == "edit"


def test_n1_mongo_legacy_unpinned_live_is_pinned():
    m, db = _mongo_store()
    m.put_definition(_spec())
    db["survey_registry"].insert_one({"_id": "rv", "status": "live"})
    assert m.get_registry("rv")["live_version"] == 1
    m.put_definition(_spec(title="later"))
    assert _q1(m.get_definition("rv")) == "Original question"


# =============================================================================== N1/N4: http backend

def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_n1_n4_http_backend_through_the_gateway(monkeypatch, tmp_path):
    uvicorn = pytest.importorskip("uvicorn")
    c = _gateway(monkeypatch, tmp_path / "definitions")
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(c.app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    try:
        for _ in range(60):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        from survey_agent import config
        from survey_agent.definitions.http import HTTPDefinitionStore
        d = HTTPDefinitionStore(base=f"http://127.0.0.1:{port}", token="api-only-token")
        assert d.put_definition(_spec()) == 1
        monkeypatch.setattr(config, "_APPROVED", None)
        with pytest.raises(urllib.error.HTTPError) as e:   # no approval forwarded → gateway refuses
            d.set_registry("rv", status="live")
        assert e.value.code == 403
        assert config.may_mark_live("human-approval")[0]
        assert d.set_registry("rv", status="live")["live_version"] == 1
        assert d.put_definition(_spec(title="edit over http")) == 2
        reg = d.get_registry("rv")
        assert (reg["live_version"], reg["latest_version"]) == (1, 2)
        assert _q1(d.get_definition("rv")) == "Original question"
        # N4 over http: an invalid spec is a 422 from the gateway, a bad id never leaves the client
        bad = _spec(); del bad["survey"]["title"]
        with pytest.raises(urllib.error.HTTPError) as e:
            d.put_definition(bad)
        assert e.value.code == 422
        for sid in ("../x", "a/b", ""):
            with pytest.raises(InvalidSurveyId):
                d.put_definition(_spec(sid=sid))
            with pytest.raises(InvalidSurveyId):
                d.delete_definition(sid)
            with pytest.raises(InvalidSurveyId):
                d.set_registry(sid, note="x")
            assert d.get_definition(sid) is None and d.get_registry(sid) is None
            assert d.iter_versions(sid) == []
    finally:
        srv.should_exit = True
        time.sleep(0.2)


# =============================================================================== N4: gateway

def test_n4_put_traversal_id_rejected_and_nothing_written_outside(monkeypatch, tmp_path):
    sdir = tmp_path / "definitions"
    c = _gateway(monkeypatch, sdir)
    for sid in ("../outside", "/abs", "a/b", "..", ""):
        r = c.put("/v1/definitions", headers=c.h, json=_spec(sid=sid))
        assert r.status_code == 422, sid
        assert r.json()["detail"]["errors"]
    assert not (tmp_path / "outside.survey.json").exists()
    assert not list(tmp_path.rglob("*.survey.json"))


def test_n4_put_invalid_spec_is_422_with_errors(monkeypatch, tmp_path):
    c = _gateway(monkeypatch, tmp_path / "definitions")
    bad = _spec(); del bad["survey"]["actions"]
    r = c.put("/v1/definitions", headers=c.h, json=bad)
    assert r.status_code == 422 and r.json()["detail"]["errors"]
    for body in ([1, 2], {"nope": 1}, {"survey": "x"}):
        assert c.put("/v1/definitions", headers=c.h, json=body).status_code == 422, body
    r = c.put("/v1/definitions", headers={**c.h, "Content-Type": "application/json"}, content=b"{not json")
    assert r.status_code == 422
    assert c.get("/v1/surveys", headers=c.h).json() == []


def test_n4_path_ids_rejected_on_read_delete_and_registry(monkeypatch, tmp_path):
    sdir = tmp_path / "definitions"
    c = _gateway(monkeypatch, sdir)
    assert c.put("/v1/definitions", headers=c.h, json=_spec()).status_code == 200
    for sid in ("a.b", "%2E%2E", "..%2Foutside", "%2Fabs"):
        assert c.get(f"/v1/definitions/{sid}", headers=c.h).status_code in (400, 404), sid
        assert c.get(f"/v1/registry/{sid}", headers=c.h).status_code in (400, 404), sid
        assert c.delete(f"/v1/definitions/{sid}", headers=c.h).status_code in (400, 404, 405), sid
        assert c.post(f"/v1/registry/{sid}", headers=c.h, json={"note": "x"}).status_code in (400, 404, 405), sid
    assert c.delete("/v1/definitions/a.b", headers=c.h).status_code == 400
    assert c.post("/v1/registry/a.b", headers=c.h, json={"note": "x"}).status_code == 400
    assert c.get("/v1/definitions/rv", headers=c.h).status_code == 200    # the real survey is untouched
    assert sorted(os.listdir(tmp_path)) == ["definitions"]


# =============================================================================== N4: file backend

@pytest.mark.parametrize("sid", BAD_IDS)
def test_n4_file_backend_rejects_bad_ids_everywhere(tmp_path, sid):
    root = tmp_path / "root"
    d = FileDefinitionStore(dir=str(root))
    d.put_definition(_spec())                              # a real neighbour that must survive
    with pytest.raises(InvalidSurveyId):
        d.put_definition(_spec(sid=sid))
    with pytest.raises(InvalidSurveyId):
        d.delete_definition(sid)
    with pytest.raises(InvalidSurveyId):
        d.set_registry(sid, status="live")
    assert d.get_definition(sid) is None
    assert d.get_definition(sid, version=1) is None
    assert d.get_registry(sid) is None
    assert d.iter_versions(sid) == []
    assert sorted(os.listdir(tmp_path)) == ["root"]
    assert d.get_definition("rv") is not None
    assert "rv" in [e["id"] for e in d.list_surveys()]


def test_n4_file_backend_refuses_paths_resolving_outside_root(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    outside.mkdir()
    d = FileDefinitionStore(dir=str(root))
    d.put_definition(_spec())
    # a symlinked snapshot dir that points out of the root is refused, not followed
    os.symlink(str(outside), str(root / ".versions" / "esc"))
    with pytest.raises(PermissionError):
        d.put_definition(_spec(sid="esc"))
    with pytest.raises(PermissionError):
        d.delete_definition("esc")
    assert os.listdir(outside) == []
    # likewise a survey file that is a symlink out of the root
    (outside / "ln.survey.json").write_text(json.dumps(_spec(sid="ln")))
    os.symlink(str(outside / "ln.survey.json"), str(root / "ln.survey.json"))
    with pytest.raises(PermissionError):
        d.put_definition(_spec(sid="ln", title="overwrite"))
    assert _q1(json.load(open(outside / "ln.survey.json"))) == "Original question"


@pytest.mark.parametrize("sid", ["../x", "/abs", "a/b", "..", ""])
def test_n4_mongo_backend_rejects_bad_ids(sid):
    m, db = _mongo_store()
    with pytest.raises(InvalidSurveyId):
        m.put_definition(_spec(sid=sid))
    with pytest.raises(InvalidSurveyId):
        m.set_registry(sid, status="live")
    with pytest.raises(InvalidSurveyId):
        m.delete_definition(sid)
    assert m.get_definition(sid) is None and m.get_registry(sid) is None and m.iter_versions(sid) == []
    assert db["survey_definitions"].count_documents({}) == 0


def test_n1_mongo_definition_without_lifecycle_is_a_draft_not_unmanaged():
    """Delete (un-gated) then re-PUT must not leave a registry-less survey the runtime treats as
    unmanaged-and-served: it reports `draft`, which the serving gate keeps closed."""
    m, _ = _mongo_store()
    m.put_definition(_spec())
    m.set_registry("rv", status="live")
    m.delete_definition("rv")
    m.put_definition(_spec(title="replacement after delete"))
    reg = m.get_registry("rv")
    assert reg["status"] == "draft" and reg.get("live_version") is None and reg["latest_version"] == 1
