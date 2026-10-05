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


def _jwt():
    import jwt  # PyJWT — part of the [auth] extra
    return jwt


def issue(email: str) -> str:
    jwt = _jwt()
    now = int(time.time())
    return jwt.encode(
        {"sub": email, "iat": now, "exp": now + TTL_SECONDS},
        config.SESSION_JWT_KEY,
        algorithm="HS256",
    )


def principal_from_request(request) -> dict | None:
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None
    try:
        jwt = _jwt()
        claims = jwt.decode(tok, config.SESSION_JWT_KEY, algorithms=["HS256"])
    except Exception:
        # No PyJWT installed, an invalid/expired/forged token, or any decode failure → no principal.
        return None
    return {"kind": "user", "email": claims.get("sub")}
