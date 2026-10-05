"""FastAPI auth dependencies for the API gateway.

A request is authenticated by EITHER an `sa_` API key (programmatic / agent / CLI) OR a Google OAuth
session cookie (humans). Either yields a `principal`; otherwise 401. The app layers the existing
single bearer `SURVEY_API_TOKEN` on top of these (see `survey_agent.api.app`).

`get_key_store` resolves the persistent (Mongo) store lazily, so importing this module never requires
pymongo or a live database — tests override it via `app.dependency_overrides`.
"""
import os

from fastapi import Depends, HTTPException, Request

from survey_agent.api.auth import apikeys, sessions

_KEY_STORE = None


def get_key_store() -> "apikeys.KeyStore":
    """The configured key store (Mongo when a deployment uses it). Overridden in tests via
    dependency_overrides; the in-memory store is the zero-config default for single-process dev."""
    global _KEY_STORE
    if _KEY_STORE is None:
        mongo = (os.environ.get("SURVEY_STORE", "memory").lower() == "mongo"
                 or os.environ.get("SURVEY_DEFINITION_SOURCE", "file").lower() == "mongo")
        _KEY_STORE = apikeys.MongoKeyStore() if mongo else apikeys.InMemoryKeyStore()
    return _KEY_STORE


def _principal_from_api_key(request: Request, store: "apikeys.KeyStore"):
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else request.headers.get("X-API-Key", "")
    if token.startswith(apikeys.PREFIX):
        return store.verify(token)
    return None


def principal_or_none(request: Request, store: "apikeys.KeyStore") -> dict | None:
    """API key OR Google session, or None — the building block of the /v1 layered gate."""
    return _principal_from_api_key(request, store) or sessions.principal_from_request(request)


def require_principal(request: Request, store: "apikeys.KeyStore" = Depends(get_key_store)) -> dict:
    principal = principal_or_none(request, store)
    if not principal:
        raise HTTPException(status_code=401, detail="authentication required (API key or Google sign-in)")
    return principal


def require_user(request: Request) -> dict:
    """A signed-in human (OAuth session), NOT an API key. Used for privileged actions like
    minting/revoking API keys, so a key can't be used to escalate by creating more keys."""
    principal = sessions.principal_from_request(request)
    if not principal:
        raise HTTPException(status_code=401, detail="Google sign-in required")
    return principal
