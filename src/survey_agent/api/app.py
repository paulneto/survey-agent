"""The FastAPI app for the survey-agent API gateway. See `survey_agent.api` for the why.

Auth is layered, and each layer is opt-in:

- **Single bearer token** (`SURVEY_API_TOKEN`) — the original zero-config gate; still works unchanged.
- **`sa_` API keys** — per-user/per-agent keys, sha256-hash-only storage (`auth.apikeys`), minted by a
  signed-in human at `/keys` or server-to-server at `/provision/cli-token`.
- **Google OAuth session** — humans sign in (`auth.oauth`); we issue a signed-JWT cookie (`auth.sessions`).

If NONE of {token, OAuth, persistent API-key store} is configured, the gateway runs WITHOUT auth
(local/dev; the CLI warns) — exactly as before. The heavy auth deps (Authlib, PyJWT, itsdangerous)
are imported lazily, so this module imports fine in plain token-only mode even without the `[auth]`
extra; the OAuth wiring only activates when a deployer configures it.

The respondent *runtime* keeps its own direct store on the hot path; this gateway is the control
plane (authoring, lifecycle, reporting reads).
"""
import hmac
import os

try:
    from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
except Exception as e:  # pragma: no cover
    raise SystemExit("the API gateway needs the [api] extra: pip install 'survey-agent[api]'") from e

from survey_agent import definitions as _definitions
from survey_agent import store as _store
from survey_agent.api.auth import config as _authcfg
from survey_agent.api.auth import deps as _authdeps
from survey_agent.api.auth import provision as _provision

app = FastAPI(title="survey-agent API", version="1",
              summary="Authenticated front door to the survey-agent data backend (definitions + responses).")


def _persistent_keystore() -> bool:
    """True when the deployment uses a persistent (Mongo) key store — i.e. API keys are 'configured'
    as an auth mechanism, not just the in-memory dev default."""
    return (os.environ.get("SURVEY_STORE", "memory").lower() == "mongo"
            or os.environ.get("SURVEY_DEFINITION_SOURCE", "file").lower() == "mongo")


def _auth_active(store=None) -> bool:
    """Any auth mechanism configured? If not, the gateway stays open (local/dev), exactly as before.

    Active when ANY of: the single bearer token is set, Google OAuth is configured, a persistent
    (Mongo) key store is in use, or at least one API key has been minted in the current store."""
    if bool(os.environ.get("SURVEY_API_TOKEN")) or _authcfg.oauth_configured() or _persistent_keystore():
        return True
    if store is not None:
        try:
            return bool(store.list())
        except Exception:
            return False
    return False


# Fail loud: a deployer who wires up Google OAuth but leaves the session key as the dev placeholder,
# unset, or too short would have forgeable sessions. Refuse to start rather than silently accept that.
# (Without OAuth, session cookies are ignored entirely — see `auth.config.session_auth_enabled`.)
if _authcfg.oauth_configured() and not _authcfg.session_key_strong():
    raise SystemExit(
        "SURVEY_GOOGLE_CLIENT_ID/SECRET are set but SURVEY_SESSION_JWT_KEY is unset, still the dev "
        f"placeholder, or shorter than {_authcfg.MIN_SESSION_KEY_LEN} characters — OAuth sessions would "
        "be forgeable. Set SURVEY_SESSION_JWT_KEY to a strong random secret "
        "(e.g. `python -c 'import secrets; print(secrets.token_urlsafe(48))'`)."
    )


def _missing_auth_deps() -> list:
    """The [auth]-extra modules that aren't importable (PyJWT, python-multipart, Authlib, itsdangerous)."""
    import importlib.util
    mods = {"jwt": "PyJWT", "multipart": "python-multipart", "authlib": "Authlib",
            "itsdangerous": "itsdangerous"}
    missing = []
    for mod, pkg in mods.items():
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append(pkg)
    return missing


# The OAuth login/callback routes are always mounted: they self-gate (503 when OAuth is unconfigured)
# and import Authlib lazily, so they cost nothing in token-only mode. The /keys UI (create/revoke API
# keys) needs a signed-in human and python-multipart for its form, so it is mounted ONLY when Google
# sign-in is configured — and imported lazily here, so a gateway installed with just `[api]` (bearer
# token / API-key mode) imports and starts without the `[auth]` extra. SessionMiddleware (Authlib's
# OAuth state/nonce across the redirect) is likewise gated on OAuth.
from survey_agent.api.auth import oauth as _oauth  # noqa: E402

app.include_router(_oauth.router)

if _authcfg.oauth_configured():
    _missing = _missing_auth_deps()
    if _missing:
        raise SystemExit(
            "Google sign-in is configured (SURVEY_GOOGLE_CLIENT_ID/SECRET) but the [auth] extra is "
            f"missing ({', '.join(_missing)}): pip install 'survey-agent[api,auth]'"
        )
    from starlette.middleware.sessions import SessionMiddleware

    from survey_agent.api.auth import keys_ui as _keys_ui

    app.include_router(_keys_ui.router)
    app.add_middleware(SessionMiddleware, secret_key=_authcfg.SESSION_JWT_KEY,
                       https_only=True, same_site="lax")


_DEFS = None
_RESP = None


def _defs():
    """The backing definition store — built once; the gateway is the only process that opens it."""
    global _DEFS
    if _DEFS is None:
        if os.environ.get("SURVEY_DEFINITION_SOURCE", "file").lower() == "http":
            raise RuntimeError("the API gateway must back onto a real store (mongo/file), not http — "
                               "run it with SURVEY_DEFINITION_SOURCE=mongo (or file)")
        _DEFS = _definitions.get_definition_store()
    return _DEFS


def _resp():
    global _RESP
    if _RESP is None:
        if os.environ.get("SURVEY_STORE", "memory").lower() == "http":
            raise RuntimeError("the API gateway must back onto a real store, not http — "
                               "run it with SURVEY_STORE=mongo (or file)")
        _RESP = _store.get_store()
    return _RESP


def _token_ok(request: Request) -> bool:
    """True if the request carries the configured single bearer SURVEY_API_TOKEN."""
    token = os.environ.get("SURVEY_API_TOKEN")
    if not token:
        return False
    got = request.headers.get("authorization", "")
    if got.startswith("Bearer "):
        got = got[7:]
    return hmac.compare_digest(got, token)


def require_auth(request: Request, store=Depends(_authdeps.get_key_store)):
    """Layered gate for every /v1 route: accept the single bearer SURVEY_API_TOKEN OR an `sa_` API key
    OR a valid OAuth session cookie. If no auth mechanism is configured at all, stay open (dev)."""
    if not _auth_active(store):
        return
    if _token_ok(request):
        return
    if _authdeps.principal_or_none(request, store):
        return
    raise HTTPException(status_code=401, detail="missing or invalid credentials (bearer token, API key, or sign-in)")


@app.get("/health")
def health(store=Depends(_authdeps.get_key_store)):
    return {"ok": True, "auth": _auth_active(store)}


@app.get("/whoami")
def whoami(request: Request, store=Depends(_authdeps.get_key_store)):
    """The current principal — token bearer, API key, signed-in user, or anonymous."""
    if _token_ok(request):
        return {"principal": {"kind": "token"}}
    principal = _authdeps.principal_or_none(request, store)
    return {"principal": principal or {"kind": "anonymous"}}


v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])


# ---- definitions seam (specs + lifecycle) ----
@v1.get("/surveys")
def list_surveys():
    return _defs().list_surveys()


@v1.get("/definitions/{survey_id}/versions")
def iter_versions(survey_id: str):
    return _defs().iter_versions(survey_id)


@v1.get("/definitions/{survey_id}")
def get_definition(survey_id: str, version: int = Query(None)):
    spec = _defs().get_definition(survey_id, version=version)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown survey")
    return spec


def _check_id(survey_id: str) -> None:
    from survey_agent.definitions.base import valid_survey_id
    if not valid_survey_id(survey_id):
        raise HTTPException(status_code=400, detail="invalid survey id: use letters, digits, '_' and '-' only")


@v1.put("/definitions")
async def put_definition(request: Request):
    """Save a definition. It is validated here (the backends never see an invalid spec) and saving
    NEVER changes what is live: the new version becomes `latest_version`; only an approved registry
    write (POST /v1/registry/{id}) moves `live_version`."""
    try:
        spec = await request.json()
    except ValueError:
        raise HTTPException(status_code=422, detail={"errors": ["body is not valid JSON"]})
    if not isinstance(spec, dict) or not isinstance(spec.get("survey"), dict):
        raise HTTPException(status_code=422, detail={"errors": ["a definition is a JSON object {\"survey\": {...}}"]})
    from survey_agent.engine.validate import validate
    try:
        errs = [e for e in validate(spec) if not str(e).startswith("[warn]")]
    except Exception as e:                     # a spec malformed enough to trip the validator itself
        errs = [f"definition could not be validated: {type(e).__name__}"]
    from survey_agent.definitions.base import valid_survey_id
    if not valid_survey_id(spec["survey"].get("id")):
        errs.append("survey.id must match ^[A-Za-z0-9_-]+$")   # enforced here even if the schema drifts
    if errs:
        raise HTTPException(status_code=422, detail={"errors": errs})
    return {"version": _defs().put_definition(spec)}


@v1.delete("/definitions/{survey_id}")
def delete_definition(survey_id: str):
    _check_id(survey_id)
    _defs().delete_definition(survey_id)
    return {"ok": True}


@v1.get("/registry/{survey_id}")
def get_registry(survey_id: str):
    reg = _defs().get_registry(survey_id)
    if reg is None:
        raise HTTPException(status_code=404, detail="unknown survey")
    return reg


# Registry fields a client may write through the gateway, each with its accepted JSON type(s). Anything
# else is rejected (400) — the registry drives lifecycle state, so it is not a free-form bag.
_REGISTRY_FIELDS = {
    "status": (str,),
    "note": (str, type(None)),
    "project": (str, type(None)),
    "live": (dict, type(None)),
    "live_version": (int, type(None)),
    "validated": (dict, type(None)),
    "tested": (dict, type(None)),
}
# Fields that set or change WHAT is live (the state itself, its launch stamp/url, the pinned version).
_LIVE_FIELDS = ("status", "live", "live_version")


def _check_registry_fields(fields: dict) -> None:
    from survey_agent import registry as _registry
    unknown = sorted(set(fields) - set(_REGISTRY_FIELDS))
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown registry field(s): {', '.join(unknown)}; "
                                                    f"allowed: {', '.join(sorted(_REGISTRY_FIELDS))}")
    for k, v in fields.items():
        if not isinstance(v, _REGISTRY_FIELDS[k]) or (k == "live_version" and isinstance(v, bool)):
            raise HTTPException(status_code=400, detail=f"registry field {k!r} has the wrong type")
    if "status" in fields and fields["status"] not in _registry.STATES:
        raise HTTPException(status_code=400, detail=f"unknown status {fields['status']!r}; "
                                                    f"choose one of: {', '.join(_registry.STATES)}")
    if isinstance(fields.get("live_version"), int) and fields["live_version"] < 1:
        raise HTTPException(status_code=400, detail="live_version must be >= 1")


def _is_live(entry: dict) -> bool:
    from survey_agent import registry as _registry
    return _registry.derived_state(entry or {}) == "live"


@v1.post("/registry/{survey_id}")
async def set_registry(survey_id: str, request: Request):
    body = await request.json()
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="registry update must be a JSON object")
    fields = dict(body)
    approve = fields.pop("approve", None) or request.headers.get("x-survey-launch-approval")
    _check_id(survey_id)
    _check_registry_fields(fields)
    # Gate on the RESULTING state, not just an explicit `status: live` — the registry also derives
    # `live` from signals like `live.at`, so judge the merged entry exactly as the store will see it.
    prior = _defs().get_registry(survey_id) or {}
    # (A store's reported `status` may itself be derived, e.g. "tested"; non-terminal states fall
    # through to the `live.at` inference in derived_state, so merging over it judges correctly.)
    result = {**prior, **fields}
    if _is_live(result) and (not _is_live(prior) or any(k in fields for k in _LIVE_FIELDS)):
        from survey_agent.config import may_mark_live
        ok, reason = may_mark_live(approve, surface="api")
        if not ok:
            raise HTTPException(status_code=403, detail=reason)
    if isinstance(fields.get("live_version"), int) and \
            _defs().get_definition(survey_id, version=fields["live_version"]) is None:
        raise HTTPException(status_code=400, detail=f"no version {fields['live_version']} of {survey_id!r}")
    _defs().set_registry(survey_id, **fields)
    return _defs().get_registry(survey_id) or {}


# ---- response store (read side; the runtime writes to its own direct store) ----
@v1.get("/responses-counts")
def response_counts():
    by = getattr(_resp(), "count_responses_by_survey", None)
    return by() if by else {}


@v1.get("/responses/{survey_id}/count")
def response_count(survey_id: str):
    cr = getattr(_resp(), "count_responses", None)
    if cr:
        return cr(survey_id)
    rows = _resp().list_responses(survey_id)
    comp = sum(1 for r in rows if r.get("status") == "Complete")
    dq = sum(1 for r in rows if r.get("status") == "Disqualified")
    return {"complete": comp, "disqualified": dq, "total": len(rows)}


@v1.get("/responses/{survey_id}")
def list_responses(survey_id: str, uid: str = Query(None)):
    return _resp().list_responses(survey_id, uid=uid)


app.include_router(v1)

# Server-to-server provisioning: a trusted SSO broker mints per-user sa_ keys here (off unless
# SURVEY_PROVISION_API_KEY is set → 404 when off). Mint-only; not under the /v1 auth gate.
app.include_router(_provision.router)
