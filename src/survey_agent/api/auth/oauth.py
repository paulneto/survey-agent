"""Google OAuth for human sign-in (the human half of the auth model).

App-level OAuth via Authlib. On a successful Google sign-in we optionally verify a single hosted
domain (only if the deployer set `SURVEY_OAUTH_ALLOWED_DOMAIN`; empty = any signed-in Google user)
and issue our own signed session cookie (sessions.py). The routes only function when a GCP client is
configured; otherwise /login and /callback return 503.

Authlib is imported LAZILY (inside `_oauth()`), so `import survey_agent.api.app` works in plain
token-only mode even when the [auth] extra is absent — the OAuth wiring only loads Authlib once a
deployer has configured OAuth and a request hits these routes.

Redirect URI to register in GCP: {SURVEY_PUBLIC_BASE_URL}/auth/google/callback
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from survey_agent.api.auth import config, sessions

router = APIRouter(prefix="/auth", tags=["auth"])

_OAUTH = None


def _oauth():
    """Build + cache the Authlib OAuth registry (lazily — only when OAuth is configured)."""
    global _OAUTH
    if _OAUTH is None:
        from authlib.integrations.starlette_client import OAuth

        oauth = OAuth()
        oauth.register(
            name="google",
            client_id=config.GOOGLE_CLIENT_ID,
            client_secret=config.GOOGLE_CLIENT_SECRET,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            # `hd` asks Google to restrict to one hosted domain; harmless/ignored when empty.
            client_kwargs={"scope": "openid email profile", "hd": config.ALLOWED_DOMAIN or None},
        )
        _OAUTH = oauth
    return _OAUTH


@router.get("/google/login")
async def login(request: Request):
    if not config.oauth_configured():
        raise HTTPException(status_code=503, detail="Google sign-in is not configured on this deployment")
    redirect_uri = config.PUBLIC_BASE_URL + config.OAUTH_REDIRECT_PATH
    return await _oauth().google.authorize_redirect(request, redirect_uri)


@router.get("/google/callback")
async def callback(request: Request):
    if not config.oauth_configured():
        raise HTTPException(status_code=503, detail="Google sign-in is not configured on this deployment")
    token = await _oauth().google.authorize_access_token(request)
    info = token.get("userinfo") or {}
    email = (info.get("email") or "").lower()
    domain = config.ALLOWED_DOMAIN.lower()
    if not email:
        raise HTTPException(status_code=403, detail="sign-in failed (no verified email)")
    # Defense in depth: the `hd` request param asks Google to restrict, but verify on our side too.
    # Empty domain = no restriction → any signed-in Google user is allowed.
    if domain and info.get("hd", "").lower() != domain and not email.endswith("@" + domain):
        raise HTTPException(status_code=403, detail=f"sign-in is restricted to @{config.ALLOWED_DOMAIN} accounts")
    resp = RedirectResponse(url="/keys", status_code=302)
    resp.set_cookie(
        sessions.COOKIE, sessions.issue(email),
        httponly=True, secure=True, samesite="lax", max_age=sessions.TTL_SECONDS,
    )
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse(url="/keys", status_code=302)
    resp.delete_cookie(sessions.COOKIE)
    return resp
