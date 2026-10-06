"""Signed session for OAuth'd users — a short-lived JWT in an httponly cookie.

Functional on its own (issue/verify the cookie); the OAuth routes that *set* it land only when a
deployer configures a GCP client. Until a user signs in there's simply no cookie, so
principal_from_request returns None.

`jwt` (PyJWT) is imported lazily so `import survey_agent.api.app` works in plain token-only mode even
when the [auth] extra is absent — verifying a cookie just no-ops (returns None) without PyJWT.
"""
import time

from survey_agent.api.auth import config

COOKIE = "survey_agent_session"
TTL_SECONDS = 12 * 3600
# Audience claim binding the token to this purpose, so no other HS256 token signed with the same key
# (now or in future) can be replayed as a session.
AUDIENCE = "survey-agent-session"


def _jwt():
    import jwt  # PyJWT — part of the [auth] extra
    return jwt


def issue(email: str) -> str:
    jwt = _jwt()
    now = int(time.time())
    return jwt.encode(
        {"sub": email, "iat": now, "exp": now + TTL_SECONDS, "aud": AUDIENCE},
        config.SESSION_JWT_KEY,
        algorithm="HS256",
    )


def principal_from_request(request) -> dict | None:
    """The signed-in user from the session cookie, or None.

    Cookies are honoured ONLY when Google sign-in is configured with a strong session key
    (`config.session_auth_enabled()`); in bearer-token / API-key / open-dev mode any cookie is ignored,
    so a token forged with the public dev key can never authenticate. A token must carry valid `exp`,
    `iat`, `aud` and a non-empty email `sub`, or it is rejected."""
    if not config.session_auth_enabled():
        return None
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None
    try:
        jwt = _jwt()
        claims = jwt.decode(
            tok, config.SESSION_JWT_KEY, algorithms=["HS256"], audience=AUDIENCE,
            options={"require": ["exp", "iat", "sub", "aud"], "verify_exp": True},
        )
    except Exception:
        # No PyJWT installed, an invalid/expired/forged token, or any decode failure → no principal.
        return None
    sub = claims.get("sub")
    if not isinstance(sub, str) or "@" not in sub or len(sub) > 320:
        return None
    if not isinstance(claims.get("iat"), int) or claims["iat"] > time.time() + 60:
        return None
    return {"kind": "user", "email": sub}
