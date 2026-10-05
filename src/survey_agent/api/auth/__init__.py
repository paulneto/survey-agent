"""Auth layer for the survey-agent API gateway.

Two credentials authenticate a request to the gateway's `/v1` data plane:

- an `sa_` **API key** (programmatic / agent / CLI) — sha256-hash-only storage, pluggable key store
  (`apikeys`), minted by signed-in humans via the `/keys` UI (`keys_ui`) or server-to-server by a
  trusted SSO broker via `/provision/cli-token` (`provision`); and
- a Google **OAuth session** cookie (humans) — `oauth` runs the Authlib handshake, `sessions` issues
  and verifies the signed-JWT cookie.

On top of these the app keeps the original single bearer `SURVEY_API_TOKEN` (`require_auth`) working
as the zero-config default. If NONE of {token, OAuth, API keys} is configured the gateway stays open
(local/dev), exactly as before.

Heavy deps (Authlib, PyJWT, itsdangerous) are imported lazily / guarded, so `import
survey_agent.api.app` works in plain token-only mode even when the `[auth]` extra is absent.
"""
