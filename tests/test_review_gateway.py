"""Regression tests for the gateway findings of the release review (R1, R2, R9).

R1 — a session cookie signed with the public dev key (or any cookie at all) must never authenticate
     unless Google sign-in is configured with a strong session key.
R2 — launch approval gates the RESULTING registry state (incl. `live` inferred from `live.at`), editable
     fields are an allowlist, and swapping `live_version` on a live survey needs approval.
R9 — the gateway imports and serves bearer-token mode with only the `[api]` extra (no python-multipart,
     PyJWT, Authlib, itsdangerous).

Everything runs against temp dirs + in-memory stores; no real surveys or credentials.
"""
import importlib
import os
import subprocess
import sys
import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

import survey_agent.api.app as appmod  # noqa: E402
from survey_agent.api.auth import apikeys, sessions  # noqa: E402
from survey_agent.api.auth import config as authcfg  # noqa: E402
from survey_agent.api.auth.deps import get_key_store  # noqa: E402

STRONG_KEY = "review-session-key-" + "k" * 40
SPEC = {"survey": {"id": "rv", "version": 1, "title": "Review", "settings": {"url_variables": ["uid"]},
                   "pages": [{"id": "p1", "questions": [{"id": "q1", "type": "text", "title": "why?"}]}],
                   "actions": [{"type": "complete", "redirect": {"kind": "default"}}]}}
_ENV = ("SURVEY_GOOGLE_CLIENT_ID", "SURVEY_GOOGLE_CLIENT_SECRET", "SURVEY_SESSION_JWT_KEY",
        "SURVEY_API_TOKEN", "SURVEY_LAUNCH_APPROVAL_TOKEN")


def _reload():
    importlib.reload(authcfg)
    return importlib.reload(appmod).app


@pytest.fixture(autouse=True)
def _restore():
    yield
    for v in _ENV:
        os.environ.pop(v, None)
    _reload()


def _gateway(monkeypatch, tmp_path, *, token=None, oauth=False, session_key=None):
    monkeypatch.setenv("SURVEY_DEFINITION_SOURCE", "file")
    monkeypatch.setenv("SURVEY_DIR", str(tmp_path / "surveys"))
    monkeypatch.setenv("SURVEY_STORE", "file")
    monkeypatch.setenv("SURVEY_DATA_DIR", str(tmp_path / "data"))
    for v in _ENV:
        monkeypatch.delenv(v, raising=False)
    if token:
        monkeypatch.setenv("SURVEY_API_TOKEN", token)
    if oauth:
        monkeypatch.setenv("SURVEY_GOOGLE_CLIENT_ID", "client-id")
        monkeypatch.setenv("SURVEY_GOOGLE_CLIENT_SECRET", "client-secret")
    if session_key:
        monkeypatch.setenv("SURVEY_SESSION_JWT_KEY", session_key)
    app = _reload()
    store = apikeys.InMemoryKeyStore()
    app.dependency_overrides[get_key_store] = lambda: store
    c = TestClient(app)
    c.keystore = store
    return c


def _forge(claims=None, key=authcfg.DEV_SESSION_KEY):
    """What an attacker can do without server access: sign a JWT with the public dev key."""
    import jwt
    now = int(time.time())
    base = {"sub": "attacker@example.org", "iat": now, "exp": now + 3600, "aud": sessions.AUDIENCE}
    if claims is not None:
        base = claims
    return jwt.encode(base, key, algorithm="HS256")


def _assert_all_protected_routes_401(c, headers=None):
    h = headers or {}
    assert c.get("/v1/surveys", headers=h).status_code == 401
    assert c.get("/v1/responses/rv", headers=h).status_code == 401
    assert c.get("/v1/responses/rv/count", headers=h).status_code == 401
    assert c.get("/v1/responses-counts", headers=h).status_code == 401
    assert c.put("/v1/definitions", json=SPEC, headers=h).status_code == 401
    assert c.delete("/v1/definitions/rv", headers=h).status_code == 401
    assert c.post("/v1/registry/rv", json={"note": "x"}, headers=h).status_code == 401
    assert c.get("/whoami", headers=h).json()["principal"]["kind"] in ("anonymous",)


# =============================================================================== R1

@pytest.mark.parametrize("forger", [
    lambda: _forge(),                                    # public dev key
    lambda: sessions.issue("attacker@example.org"),      # whatever key the server would use
])
def test_r1_bearer_only_mode_ignores_forged_cookie(monkeypatch, tmp_path, forger):
    pytest.importorskip("jwt")
    c = _gateway(monkeypatch, tmp_path, token="synthetic-review-token")
    c.cookies.set(sessions.COOKIE, forger())
    _assert_all_protected_routes_401(c)
    # the real bearer still works
    assert c.get("/v1/surveys", headers={"Authorization": "Bearer synthetic-review-token"}).status_code == 200


def test_r1_api_key_only_mode_ignores_forged_cookie(monkeypatch, tmp_path):
    pytest.importorskip("jwt")
    c = _gateway(monkeypatch, tmp_path)
    key = c.keystore.add("agent")                        # API keys exist → auth is on
    c.cookies.set(sessions.COOKIE, _forge())
    _assert_all_protected_routes_401(c)
    assert c.get("/v1/surveys", headers={"Authorization": f"Bearer {key}"}).status_code == 200


def test_r1_key_admin_routes_reject_forged_cookie(monkeypatch, tmp_path):
    pytest.importorskip("jwt")
    c = _gateway(monkeypatch, tmp_path, token="synthetic-review-token")
    c.keystore.add("agent")
    kid = c.keystore.list()[0]["id"]
    c.cookies.set(sessions.COOKIE, _forge())
    # No OAuth → the key UI isn't mounted; nothing can create or revoke a key through it.
    assert c.post("/keys", data={"label": "evil"}).status_code in (401, 404, 405)
    assert c.post(f"/keys/{kid}/revoke").status_code in (401, 404, 405)
    assert len(c.keystore.list()) == 1 and not c.keystore.list()[0].get("disabled")


def test_r1_oauth_with_weak_or_default_key_refuses_to_start(monkeypatch, tmp_path):
    for weak in (None, authcfg.DEV_SESSION_KEY, "short-but-not-default"):
        with pytest.raises(SystemExit):
            _gateway(monkeypatch, tmp_path, oauth=True, session_key=weak)


def test_r1_oauth_mode_valid_cookie_works_forged_and_malformed_rejected(monkeypatch, tmp_path):
    pytest.importorskip("jwt")
    pytest.importorskip("itsdangerous")
    pytest.importorskip("multipart")
    pytest.importorskip("authlib")
    c = _gateway(monkeypatch, tmp_path, oauth=True, session_key=STRONG_KEY)
    now = int(time.time())
    bad = {
        "dev-key": _forge(),
        "no-sub": _forge({"iat": now, "exp": now + 60, "aud": sessions.AUDIENCE}, key=STRONG_KEY),
        "empty-sub": _forge({"sub": "", "iat": now, "exp": now + 60, "aud": sessions.AUDIENCE}, key=STRONG_KEY),
        "non-email-sub": _forge({"sub": {"x": 1}, "iat": now, "exp": now + 60, "aud": sessions.AUDIENCE},
                                key=STRONG_KEY),
        "no-exp": _forge({"sub": "a@example.org", "iat": now, "aud": sessions.AUDIENCE}, key=STRONG_KEY),
        "expired": _forge({"sub": "a@example.org", "iat": now - 7200, "exp": now - 3600,
                           "aud": sessions.AUDIENCE}, key=STRONG_KEY),
        "no-aud": _forge({"sub": "a@example.org", "iat": now, "exp": now + 60}, key=STRONG_KEY),
        "garbage": "not.a.jwt",
    }
    for name, tok in bad.items():
        c.cookies.clear()
        c.cookies.set(sessions.COOKIE, tok)
        assert c.get("/v1/surveys").status_code == 401, name
        assert c.post("/keys", data={"label": "x"}).status_code == 401, name
    # a genuine session issued by the server works for /v1 and for key admin
    c.cookies.clear()
    c.cookies.set(sessions.COOKIE, sessions.issue("jane@example.com"))
    assert c.get("/v1/surveys").status_code == 200
    assert c.post("/keys", data={"label": "jane"}).status_code == 200
    assert len(c.keystore.list()) == 1


# =============================================================================== R2

@pytest.fixture
def reg_client(monkeypatch, tmp_path):
    c = _gateway(monkeypatch, tmp_path, token="sek")
    monkeypatch.setenv("SURVEY_LAUNCH_APPROVAL_TOKEN", "human-ok")
    c.h = {"Authorization": "Bearer sek"}
    c.ok = {**c.h, "X-Survey-Launch-Approval": "human-ok"}
    assert c.put("/v1/definitions", headers=c.h, json=SPEC).status_code == 200
    return c


def test_r2_live_at_cannot_bypass_approval(reg_client):
    c = reg_client
    r = c.post("/v1/registry/rv", headers=c.h, json={"live": {"at": "synthetic-date"}})
    assert r.status_code == 403
    assert c.get("/v1/registry/rv", headers=c.h).json()["status"] != "live"
    assert c.post("/v1/registry/rv", headers=c.h, json={"status": "live"}).status_code == 403
    assert c.post("/v1/registry/rv", headers=c.h,
                  json={"live": {"at": "x"}, "approve": "wrong"}).status_code == 403
    r = c.post("/v1/registry/rv", headers=c.ok, json={"live": {"at": "synthetic-date"}})
    assert r.status_code == 200 and r.json()["status"] == "live"


def test_r2_live_at_blocked_without_any_approval_token(monkeypatch, tmp_path):
    c = _gateway(monkeypatch, tmp_path, token="sek")
    h = {"Authorization": "Bearer sek"}
    c.put("/v1/definitions", headers=h, json=SPEC)
    assert c.post("/v1/registry/rv", headers=h, json={"live": {"at": "x"}}).status_code == 403


def test_r2_unknown_and_badly_typed_fields_rejected(reg_client):
    c = reg_client
    for body in ({"foo": 1}, {"status": "bogus"}, {"live": "yes"}, {"live_version": "2"},
                 {"live_version": True}, {"live_version": 0}, {"note": 5}):
        assert c.post("/v1/registry/rv", headers=c.h, json=body).status_code == 400, body
    assert c.post("/v1/registry/rv", headers=c.h, json=[1, 2]).status_code == 400


def test_r2_live_survey_edits(reg_client):
    c = reg_client
    assert c.post("/v1/registry/rv", headers=c.ok, json={"status": "live", "live_version": 1}).status_code == 200
    # benign edits on a live survey need no approval and keep it live
    r = c.post("/v1/registry/rv", headers=c.h, json={"note": "fielding", "project": "q4"})
    assert r.status_code == 200 and r.json()["status"] == "live"
    # swapping the pinned version of a live survey substitutes content → approval required
    assert c.post("/v1/registry/rv", headers=c.h, json={"live_version": 2}).status_code == 403
    assert c.post("/v1/registry/rv", headers=c.ok, json={"live_version": 1}).status_code == 200
    # re-stamping `live` on a live survey is also a launch act
    assert c.post("/v1/registry/rv", headers=c.h, json={"live": {"at": "later"}}).status_code == 403
    # taking it OUT of live never needs approval
    r = c.post("/v1/registry/rv", headers=c.h, json={"status": "paused"})
    assert r.status_code == 200 and r.json()["status"] == "paused"
    # ...and putting it back does
    assert c.post("/v1/registry/rv", headers=c.h, json={"status": "live"}).status_code == 403


def test_r2_non_live_lifecycle_writes_still_work(reg_client):
    c = reg_client
    r = c.post("/v1/registry/rv", headers=c.h,
               json={"validated": {"ok": True, "at": "t"}, "tested": {"gate": "passed", "at": "t"}})
    assert r.status_code == 200 and r.json()["status"] == "tested"
    assert c.post("/v1/registry/rv", headers=c.h, json={"status": "approved"}).json()["status"] == "approved"
    # a URL without a launch stamp doesn't make it live
    r = c.post("/v1/registry/rv", headers=c.h, json={"status": "draft", "live": {"url": "https://example.org/s"}})
    assert r.status_code == 200 and r.json()["status"] != "live"


# =============================================================================== R9

_BLOCKER = r"""
import sys, importlib.abc
BLOCKED = {"multipart", "python_multipart", "jwt", "authlib", "itsdangerous"}
class _Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in BLOCKED:
            raise ModuleNotFoundError(f"blocked for test: {name}", name=name)
        return None
sys.meta_path.insert(0, _Block())
for m in list(sys.modules):
    if m.split(".")[0] in BLOCKED:
        del sys.modules[m]
"""


def test_r9_gateway_imports_and_serves_without_auth_extra(tmp_path):
    code = _BLOCKER + r"""
import os
os.environ.update(SURVEY_DEFINITION_SOURCE="file", SURVEY_STORE="memory",
                  SURVEY_DIR=os.environ["T"], SURVEY_API_TOKEN="sek")
for v in ("SURVEY_GOOGLE_CLIENT_ID", "SURVEY_GOOGLE_CLIENT_SECRET"):
    os.environ.pop(v, None)
import survey_agent.api.app as a
from fastapi.testclient import TestClient
c = TestClient(a.app)
assert c.get("/v1/surveys").status_code == 401
assert c.get("/v1/surveys", headers={"Authorization": "Bearer sek"}).status_code == 200
c.cookies.set("survey_agent_session", "anything")
assert c.get("/v1/surveys").status_code == 401
assert c.get("/keys").status_code == 404
print("OK")
"""
    env = {**os.environ, "T": str(tmp_path)}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr[-2000:]


def test_r9_oauth_without_auth_extra_fails_loud(tmp_path):
    code = _BLOCKER + r"""
import os
os.environ.update(SURVEY_DEFINITION_SOURCE="file", SURVEY_STORE="memory", SURVEY_DIR=os.environ["T"],
                  SURVEY_GOOGLE_CLIENT_ID="id", SURVEY_GOOGLE_CLIENT_SECRET="secret",
                  SURVEY_SESSION_JWT_KEY="x" * 48)
try:
    import survey_agent.api.app
except SystemExit as e:
    print("EXIT", e)
"""
    env = {**os.environ, "T": str(tmp_path)}
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert "EXIT" in r.stdout and "[auth]" in r.stdout, (r.stdout, r.stderr[-2000:])
