"""Config for the survey-agent API gateway's auth layer.

Reads env only; secrets are injected by the deployer's environment / secret manager. Nothing here
is a secret default beyond the dev-only session key (which fail-loud rejects in production once OAuth
is configured — see `survey_agent.api.app`).

Every var is `SURVEY_*` prefixed — the open core stays vendor-neutral. `SURVEY_OAUTH_ALLOWED_DOMAIN`
defaults to **empty** on purpose: empty means any signed-in Google user is allowed. A deployer who
wants to lock sign-in to a single hosted domain sets it explicitly; the core never ships a default.
"""
import os

# A dev placeholder we explicitly refuse once OAuth is live (forgeable sessions otherwise).
DEV_SESSION_KEY = "dev-only-change-me"

# Google OAuth (humans). Empty until the deployer creates a GCP client — oauth_configured() gates it.
GOOGLE_CLIENT_ID = os.environ.get("SURVEY_GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("SURVEY_GOOGLE_CLIENT_SECRET", "")
# Optional single Google hosted-domain (hd) allowlist. EMPTY = any signed-in Google user is allowed.
# The open core never ships a default domain — set it yourself to lock sign-in to one org.
ALLOWED_DOMAIN = os.environ.get("SURVEY_OAUTH_ALLOWED_DOMAIN", "")

# Signed-session key for OAuth'd users (JWT cookie + the Authlib redirect session). Must be set to a
# real secret (>= MIN_SESSION_KEY_LEN chars) in any deployment that turns OAuth on; a weak/placeholder
# key is rejected at startup then. Without OAuth, session cookies are never honoured at all.
SESSION_JWT_KEY = os.environ.get("SURVEY_SESSION_JWT_KEY", DEV_SESSION_KEY)

# Server-to-server provisioning key (the trust boundary for /provision/cli-token). A trusted upstream
# SSO broker presents this — after it has verified a Google-SSO user — to mint that user a per-user
# `sa_` key on their behalf. Mint-only: never accepted on any /v1 auth path. Empty/unset → the
# provisioning endpoint is off (404), so the feature is invisible until a deployer enables it.
PROVISION_API_KEY = os.environ.get("SURVEY_PROVISION_API_KEY", "")

# Public base; the OAuth redirect is a path on the same host. Reuses the engine's existing base-url var.
PUBLIC_BASE_URL = os.environ.get("SURVEY_PUBLIC_BASE_URL", "http://localhost:8000")
OAUTH_REDIRECT_PATH = "/auth/google/callback"


# A session key shorter than this is treated as weak (HS256 wants >= 256 bits of key material).
MIN_SESSION_KEY_LEN = 32


def oauth_configured() -> bool:
    """OAuth is live only when a GCP client id + secret are both present."""
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def session_key_strong(key=None) -> bool:
    """True when the session signing key is a real secret: set, not the public dev placeholder, and
    at least MIN_SESSION_KEY_LEN characters long."""
    key = SESSION_JWT_KEY if key is None else key
    return bool(key) and key != DEV_SESSION_KEY and len(key) >= MIN_SESSION_KEY_LEN


def session_auth_enabled() -> bool:
    """Session cookies are honoured ONLY when Google sign-in is configured AND the session key is
    strong. In every other mode (bearer-token only, API-key only, open dev) cookies are ignored
    entirely — otherwise anyone could forge one with the public dev key."""
    return oauth_configured() and session_key_strong()
