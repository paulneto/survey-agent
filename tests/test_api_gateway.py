"""The API gateway (FastAPI) + the stdlib HTTP seam backends — the console/agent/CLI reach the data
backend through one authenticated surface instead of a direct DB connection."""
import importlib
import socket
import threading
import time
import urllib.request

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

SPEC = {"survey": {"id": "g1", "version": 1, "title": "G1", "tags": ["t"],
                   "settings": {"url_variables": ["uid"]},
                   "pages": [{"id": "p1", "questions": [{"id": "q1", "type": "text", "title": "why?"}]}],
                   "actions": [{"type": "complete", "redirect": {"kind": "default"}}]}}


def _app(tmp_path, monkeypatch, token="sek"):
    """A gateway bound to a fresh file backing (reload resets the cached store globals)."""
    monkeypatch.setenv("SURVEY_DEFINITION_SOURCE", "file")
    monkeypatch.setenv("SURVEY_DIR", str(tmp_path))
    monkeypatch.setenv("SURVEY_STORE", "file")
    monkeypatch.setenv("SURVEY_DATA_DIR", str(tmp_path / "data"))
    if token:
        monkeypatch.setenv("SURVEY_API_TOKEN", token)
    else:
        monkeypatch.delenv("SURVEY_API_TOKEN", raising=False)
    import survey_agent.api.app as appmod
    importlib.reload(appmod)
    return appmod.app


def test_gateway_routes_and_bearer_auth(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    client = TestClient(_app(tmp_path, monkeypatch, token="sek"))
    assert client.get("/health").json()["ok"] is True                      # health is open
    assert client.get("/v1/surveys").status_code == 401                    # no token → blocked
    h = {"Authorization": "Bearer sek"}
    assert client.get("/v1/surveys", headers=h).json() == []
    assert client.put("/v1/definitions", headers=h, json=SPEC).json() == {"version": 1}
    assert client.get("/v1/definitions/g1", headers=h).json()["survey"]["title"] == "G1"
    assert client.post("/v1/registry/g1", headers=h, json={"status": "tested"}).json()["status"] == "tested"
    assert [s["id"] for s in client.get("/v1/surveys", headers=h).json()] == ["g1"]
    assert client.get("/v1/definitions/nope", headers=h).status_code == 404
    assert client.delete("/v1/definitions/g1", headers=h).json() == {"ok": True}
    assert client.get("/v1/definitions/g1", headers=h).status_code == 404


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_http_seam_clients_round_trip(tmp_path, monkeypatch):
    import uvicorn
    app = _app(tmp_path, monkeypatch, token="sek")
    port = _free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    try:
        for _ in range(60):                       # wait for readiness
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        monkeypatch.setenv("SURVEY_API_BASE", f"http://127.0.0.1:{port}")
        from survey_agent.definitions.http import HTTPDefinitionStore
        from survey_agent.store.http import HTTPStore
        d, s = HTTPDefinitionStore(), HTTPStore()
        assert d.put_definition(SPEC) == 1                                  # write through the gateway
        assert d.get_definition("g1")["survey"]["title"] == "G1"
        monkeypatch.setenv("SURVEY_LAUNCH_APPROVAL_TOKEN", "human-ok")
        from survey_agent import config
        assert config.may_mark_live("human-ok")[0]                          # the CLI's local check…
        d.set_registry("g1", status="live")                                 # …is forwarded to the gateway
        assert d.get_registry("g1")["status"] == "live"
        assert [e["id"] for e in d.list_surveys()] == ["g1"]
        assert d.get_definition("nope") is None                            # 404 → None
        assert s.count_responses_by_survey() == {}                         # read side works
        with pytest.raises(NotImplementedError):                           # write side is refused (control plane)
            s.record_response("g1", {"status": "Complete"})
        assert d.delete_definition("g1") is True and d.get_definition("g1") is None
    finally:
        srv.should_exit = True
        time.sleep(0.2)


def test_factories_resolve_http_backend(monkeypatch):
    monkeypatch.setenv("SURVEY_DEFINITION_SOURCE", "http")
    monkeypatch.setenv("SURVEY_STORE", "http")
    from survey_agent import definitions, store
    from survey_agent.definitions.http import HTTPDefinitionStore
    from survey_agent.store.http import HTTPStore
    assert isinstance(definitions.get_definition_store(), HTTPDefinitionStore)
    assert isinstance(store.get_store(), HTTPStore)


def test_gateway_marks_live_only_with_the_approval_token(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.delenv("SURVEY_LAUNCH_APPROVAL_TOKEN", raising=False)
    client = TestClient(_app(tmp_path, monkeypatch, token="sek"))
    h = {"Authorization": "Bearer sek"}
    client.put("/v1/definitions", headers=h, json=SPEC)
    # no approval token configured → the gateway (a remote surface) can never mark live
    assert client.post("/v1/registry/g1", headers=h, json={"status": "live"}).status_code == 403
    monkeypatch.setenv("SURVEY_LAUNCH_APPROVAL_TOKEN", "human-ok")
    assert client.post("/v1/registry/g1", headers=h, json={"status": "live"}).status_code == 403
    assert client.post("/v1/registry/g1", headers=h, json={"status": "live", "approve": "nope"}).status_code == 403
    r = client.post("/v1/registry/g1", headers={**h, "X-Survey-Launch-Approval": "human-ok"}, json={"status": "live"})
    assert r.status_code == 200 and r.json()["status"] == "live"
