"""Auth layer for the API gateway — the sa_ API-key half, the OAuth session half, the /keys UI, and
the server-to-server /provision/cli-token mint.

Skips cleanly if the [auth] extra is absent (mirrors how test_mcp_tools.py skips without its SDK):
the cookie path needs PyJWT, the /keys form POST needs python-multipart, and the whole thing needs
fastapi. All three are part of `survey-agent[auth]` (+ [api])."""
import importlib
import os
import re

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("jwt")        # PyJWT — issues/verifies the session cookie
pytest.importorskip("multipart")  # python-multipart — fastapi Form parsing for the /keys UI

import logging  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

# A file definition store against a throwaway dir keeps these tests off any real Mongo.
os.environ.setdefault("SURVEY_DEFINITION_SOURCE", "file")
os.environ.setdefault("SURVEY_STORE", "memory")

import survey_agent.api.app as appmod  # noqa: E402
from survey_agent.api.auth import apikeys, config, sessions  # noqa: E402
from survey_agent.api.auth.deps import get_key_store  # noqa: E402

app = appmod.app


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Fresh in-memory key store per test; the engine store points at a throwaway dir."""
    monkeypatch.setenv("SURVEY_DEFINITION_SOURCE", "file")
    monkeypatch.setenv("SURVEY_DIR", str(tmp_path))
    monkeypatch.delenv("SURVEY_API_TOKEN", raising=False)
    store = apikeys.InMemoryKeyStore()
    app.dependency_overrides[get_key_store] = lambda: store
    c = TestClient(app)
    c.keystore = store
    yield c
    app.dependency_overrides.clear()


STRONG_SESSION_KEY = "test-session-key-0123456789abcdefghijklmnopqrstuvwxyz"


def _reload_app():
    importlib.reload(importlib.import_module("survey_agent.api.auth.config"))
    return importlib.reload(appmod).app


@pytest.fixture
def oauth_client(monkeypatch, tmp_path):
    """Gateway with Google sign-in configured and a strong session key — the ONLY mode in which
    session cookies are honoured (and the /keys UI is mounted)."""
    pytest.importorskip("itsdangerous")
    pytest.importorskip("authlib")
    monkeypatch.setenv("SURVEY_DEFINITION_SOURCE", "file")
    monkeypatch.setenv("SURVEY_DIR", str(tmp_path))
    monkeypatch.delenv("SURVEY_API_TOKEN", raising=False)
    monkeypatch.setenv("SURVEY_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("SURVEY_GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SURVEY_SESSION_JWT_KEY", STRONG_SESSION_KEY)
    oapp = _reload_app()
    store = apikeys.InMemoryKeyStore()
    oapp.dependency_overrides[get_key_store] = lambda: store
    c = TestClient(oapp)
    c.keystore = store
    yield c
    oapp.dependency_overrides.clear()


# ---- health / whoami ----

def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_whoami_anonymous(client):
    # no token / oauth / keys → open dev mode; whoami reports anonymous
    assert client.get("/whoami").json()["principal"]["kind"] == "anonymous"


# ---- sa_ API-key auth on /v1 ----

def test_api_key_authenticates(client):
    key = client.keystore.add("test-agent")
    assert key.startswith("sa_")
    r = client.get("/whoami", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    p = r.json()["principal"]
    assert p["kind"] == "api_key" and p["label"] == "test-agent"


def test_v1_open_until_a_key_exists_then_gated(client):
    # zero-config dev: no token, no oauth, no keys → /v1 stays open
    assert client.get("/v1/surveys").status_code == 200
    # minting a key turns auth on → an unauthenticated request is now rejected
    key = client.keystore.add("agent")
    assert client.get("/v1/surveys").status_code == 401
    r = client.get("/v1/surveys", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200


def test_bad_key_rejected(client):
    client.keystore.add("agent")  # turn auth on
    assert client.get("/v1/surveys", headers={"Authorization": "Bearer sa_not_a_real_key"}).status_code == 401


def test_revoked_key_rejected(client):
    key = client.keystore.add("temp")
    kid = client.keystore.list()[0]["id"]
    assert client.keystore.revoke(kid) is True
    assert client.get("/v1/surveys", headers={"Authorization": f"Bearer {key}"}).status_code == 401


def test_x_api_key_header_also_works(client):
    key = client.keystore.add("agent")
    r = client.get("/v1/surveys", headers={"X-API-Key": key})
    assert r.status_code == 200


# ---- single bearer SURVEY_API_TOKEN still gates (zero-config default unchanged) ----

def test_single_bearer_token_gate(client, monkeypatch):
    monkeypatch.setenv("SURVEY_API_TOKEN", "sek")
    assert client.get("/v1/surveys").status_code == 401
    assert client.get("/v1/surveys", headers={"Authorization": "Bearer sek"}).status_code == 200
    assert client.get("/whoami", headers={"Authorization": "Bearer sek"}).json()["principal"]["kind"] == "token"


# ---- OAuth session-cookie half (only with Google sign-in + a strong session key) ----

def test_session_cookie_authenticates_as_user(oauth_client):
    oauth_client.cookies.set(sessions.COOKIE, sessions.issue("jane@example.com"))
    r = oauth_client.get("/whoami")
    assert r.status_code == 200
    p = r.json()["principal"]
    assert p["kind"] == "user" and p["email"] == "jane@example.com"
    oauth_client.cookies.clear()


def test_session_cookie_authenticates_v1(oauth_client):
    assert oauth_client.get("/v1/surveys").status_code == 401        # OAuth on → /v1 gated
    oauth_client.cookies.set(sessions.COOKIE, sessions.issue("jane@example.com"))
    assert oauth_client.get("/v1/surveys").status_code == 200
    oauth_client.cookies.clear()


def test_session_cookie_ignored_without_oauth(client):
    # Bearer/API-key mode: a cookie (even one signed with the server's own key) is never a principal.
    client.keystore.add("agent")
    client.cookies.set(sessions.COOKIE, sessions.issue("jane@example.com"))
    assert client.get("/v1/surveys").status_code == 401
    assert client.get("/whoami").json()["principal"]["kind"] == "anonymous"
    client.cookies.clear()


def test_login_503_when_oauth_unconfigured(client):
    # tests run with no GCP client → sign-in cleanly unavailable (503, not 500/404)
    assert client.get("/auth/google/login", follow_redirects=False).status_code == 503


# ---- /keys UI: sign-in-only, no privilege escalation from a key ----

def test_keys_ui_not_mounted_without_oauth(client):
    # No Google sign-in → nobody can be a signed-in human → the key UI isn't served at all.
    assert client.get("/keys", follow_redirects=False).status_code == 404
    assert client.post("/keys", data={"label": "x"}).status_code in (404, 405)


def test_keys_page_redirects_anonymous(oauth_client):
    r = oauth_client.get("/keys", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/auth/google/login"


def test_keys_create_is_user_only_not_api_key(oauth_client):
    # an API key must NOT be able to mint keys (no privilege escalation)
    key = oauth_client.keystore.add("agent")
    r = oauth_client.post("/keys", data={"label": "x"}, headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 401


def test_keys_create_and_revoke_as_user(oauth_client):
    import re

    client = oauth_client
    client.cookies.set(sessions.COOKIE, sessions.issue("jane@example.com"))
    assert client.get("/keys").status_code == 200                      # page renders for a signed-in user
    r = client.post("/keys", data={"label": "jane-cli"})               # create → key shown once
    assert r.status_code == 200 and "sa_" in r.text and "jane-cli" in r.text
    m = re.search(r"sa_[A-Za-z0-9_-]+", r.text)
    assert m
    kid = client.keystore.list()[0]["id"]
    assert client.post(f"/keys/{kid}/revoke", follow_redirects=False).status_code == 302
    # drop the session cookie so only the key authenticates — proves the revoke actually killed it
    client.cookies.clear()
    assert client.get("/v1/surveys", headers={"Authorization": f"Bearer {m.group(0)}"}).status_code == 401


# ---- de-branding sanity: the UI names no third-party host or brand ----

# Hosts that may legitimately appear in the page (placeholders / the test client itself).
_ALLOWED_HOSTS = {"example.com", "example.org", "example.net", "localhost", "127.0.0.1", "testserver"}


def _extra_denylist():
    """Optional deployer-supplied words that must never appear (comma-separated in
    SURVEY_TEST_DENYLIST, or one per line in a gitignored tests/.denylist). Nothing is named here."""
    words = [w for w in os.environ.get("SURVEY_TEST_DENYLIST", "").split(",") if w.strip()]
    path = os.path.join(os.path.dirname(__file__), ".denylist")
    if os.path.exists(path):
        with open(path) as f:
            words += [ln for ln in f.read().splitlines() if ln.strip() and not ln.startswith("#")]
    return [w.strip().lower() for w in words]


def test_keys_ui_is_vendor_neutral(oauth_client):
    oauth_client.cookies.set(sessions.COOKIE, sessions.issue("jane@example.com"))
    body = oauth_client.get("/keys").text.lower()
    oauth_client.cookies.clear()
    hosts = set(re.findall(r"https?://([a-z0-9.-]+)", body)) | set(re.findall(r"@([a-z0-9-]+(?:\.[a-z0-9-]+)+)", body))
    for h in hosts:
        assert h in _ALLOWED_HOSTS or any(h.endswith("." + a) for a in _ALLOWED_HOSTS), h
    for word in _extra_denylist():
        assert word not in body, "denylisted word in the keys UI"


# =====================================================================================
# Server-to-server provisioning — POST /provision/cli-token
# =====================================================================================

PROVISION_KEY = "test-provision-secret-123"


@pytest.fixture
def prov_client(monkeypatch, tmp_path):
    monkeypatch.setenv("SURVEY_DEFINITION_SOURCE", "file")
    monkeypatch.setenv("SURVEY_DIR", str(tmp_path))
    monkeypatch.setattr(config, "PROVISION_API_KEY", PROVISION_KEY)
    store = apikeys.InMemoryKeyStore()
    app.dependency_overrides[get_key_store] = lambda: store
    c = TestClient(app)
    c.keystore = store
    yield c
    app.dependency_overrides.clear()


def _hdr(key=PROVISION_KEY):
    return {"Authorization": f"Bearer {key}"}


def test_valid_key_mints_per_user_key(prov_client):
    r = prov_client.post("/provision/cli-token", json={"email": "jane@example.com"}, headers=_hdr())
    assert r.status_code == 200
    body = r.json()
    assert body["token"].startswith("sa_")
    assert body["email"] == "jane@example.com"
    assert body["label"] == "cli"
    assert body["id"]
    # the minted key works as a real gateway API key, bound to the user's email
    who = prov_client.get("/whoami", headers={"Authorization": f"Bearer {body['token']}"})
    assert who.status_code == 200
    p = who.json()["principal"]
    assert p["kind"] == "api_key" and p["email"] == "jane@example.com"


def test_custom_label_is_honored(prov_client):
    r = prov_client.post(
        "/provision/cli-token", json={"email": "bob@example.com", "label": "bob-laptop"}, headers=_hdr()
    )
    assert r.status_code == 200 and r.json()["label"] == "bob-laptop"


def test_missing_key_is_401(prov_client):
    assert prov_client.post("/provision/cli-token", json={"email": "jane@example.com"}).status_code == 401


def test_bad_key_is_401(prov_client):
    r = prov_client.post(
        "/provision/cli-token", json={"email": "jane@example.com"}, headers=_hdr("wrong-key")
    )
    assert r.status_code == 401


def test_malformed_bearer_is_401(prov_client):
    r = prov_client.post(
        "/provision/cli-token",
        json={"email": "jane@example.com"},
        headers={"Authorization": PROVISION_KEY},  # no "Bearer " scheme
    )
    assert r.status_code == 401


def test_off_when_unconfigured_is_404(monkeypatch, tmp_path):
    # Empty provisioning key → feature off → 404, regardless of what bearer is sent.
    monkeypatch.setenv("SURVEY_DEFINITION_SOURCE", "file")
    monkeypatch.setenv("SURVEY_DIR", str(tmp_path))
    monkeypatch.setattr(config, "PROVISION_API_KEY", "")
    store = apikeys.InMemoryKeyStore()
    app.dependency_overrides[get_key_store] = lambda: store
    c = TestClient(app)
    try:
        r = c.post("/provision/cli-token", json={"email": "jane@example.com"}, headers=_hdr())
        assert r.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_provision_key_cannot_authenticate_v1(prov_client):
    # PRIVILEGE SEPARATION: the provisioning key is mint-only — it must NOT pass as a /v1 API key
    # (it lacks the sa_ prefix and is a different secret entirely).
    prov_client.keystore.add("agent")  # turn auth on so /v1 is actually gated
    r = prov_client.get("/v1/surveys", headers=_hdr())
    assert r.status_code == 401
    # ...and it isn't a recognized principal either
    assert prov_client.get("/whoami", headers=_hdr()).json()["principal"]["kind"] == "anonymous"


def test_never_logs_token_or_provisioning_key(prov_client, caplog):
    with caplog.at_level(logging.DEBUG):
        r = prov_client.post(
            "/provision/cli-token", json={"email": "jane@example.com"}, headers=_hdr()
        )
    token = r.json()["token"]
    logged = "\n".join(rec.getMessage() for rec in caplog.records)
    assert token not in logged
    assert PROVISION_KEY not in logged


# ---- fail-loud startup guard: OAuth configured but session key left as the dev placeholder ----

def test_fail_loud_when_oauth_configured_without_session_key(monkeypatch):
    monkeypatch.setenv("SURVEY_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("SURVEY_GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.delenv("SURVEY_SESSION_JWT_KEY", raising=False)  # → dev placeholder
    with pytest.raises(SystemExit):
        importlib.reload(importlib.import_module("survey_agent.api.auth.config"))
        importlib.reload(appmod)


def test_fail_loud_when_oauth_configured_with_short_session_key(monkeypatch):
    monkeypatch.setenv("SURVEY_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("SURVEY_GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SURVEY_SESSION_JWT_KEY", "too-short-secret")
    with pytest.raises(SystemExit):
        _reload_app()


def test_oauth_with_session_key_imports_ok(monkeypatch):
    # The mirror of the fail-loud test: a real session key lets the OAuth-configured app import.
    pytest.importorskip("itsdangerous")  # SessionMiddleware dep — only present with the [auth] extra
    monkeypatch.setenv("SURVEY_GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("SURVEY_GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("SURVEY_SESSION_JWT_KEY", STRONG_SESSION_KEY)
    importlib.reload(importlib.import_module("survey_agent.api.auth.config"))
    reloaded = importlib.reload(appmod)
    assert reloaded.app is not None


@pytest.fixture(autouse=True)
def _restore_app_module():
    """Some tests reload survey_agent.api.app / .auth.config with OAuth env set; restore the clean
    token-only module afterward so the module-level `app` other tests use stays consistent."""
    yield
    for var in ("SURVEY_GOOGLE_CLIENT_ID", "SURVEY_GOOGLE_CLIENT_SECRET", "SURVEY_SESSION_JWT_KEY"):
        os.environ.pop(var, None)
    importlib.reload(importlib.import_module("survey_agent.api.auth.config"))
    importlib.reload(appmod)
    globals()["app"] = appmod.app
