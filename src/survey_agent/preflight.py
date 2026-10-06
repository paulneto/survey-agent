"""Operator pre-fielding checklist — the responsibilities surfaced at launch.

survey-agent is run and hosted by a trusted operator who owns privacy, security, and the data
lifecycle (see docs/OPERATOR_RESPONSIBILITIES.md and docs/DATA_SECURITY.md). The engine never owns
respondent data, so it can't make these guarantees for you. Launching fields to real people — so the
launch surfaces (CLI `launch`, MCP `launch_survey`) print this checklist, and a token-gated
deployment additionally requires an explicit acknowledgement before flipping a survey live.

Pure Python, no deps — shared by `cli._cmd_launch` and `mcp.server.launch_survey`.
"""

DOC = "docs/OPERATOR_RESPONSIBILITIES.md"


def doc_ref(path):
    """A docs pointer that works where the user is: the repo path in a source checkout, else the
    canonical URL (a pip install has no docs/ folder). Routed through survey_agent.docs."""
    try:
        from survey_agent import docs
        kind, loc = docs.resolve(path)
        return path if kind == "file" else loc
    except Exception:  # pragma: no cover
        return path

# (item, why-it-matters) — kept short enough to read at the moment of launch.
RESPONSIBILITIES = [
    ("Response store",
     "A durable store is configured and backed up (SURVEY_STORE = file on a persistent disk, or "
     "mongo / your own DB). The in-memory store loses everything on restart."),
    ("Erasure / DSAR",
     "You can find and erase a subject's responses by uid: `survey-agent dsar <uid>` lists them and "
     "`--delete` erases them from the configured store. Copies you hold elsewhere (exports, backups, "
     "downstream systems) are still yours to clear."),
    ("Consent & disclosure",
     "The survey's consent/notice text is accurate for your jurisdiction, and any LLM analysis or "
     "conversational mode is disclosed to respondents."),
    ("URL integrity",
     "If any URL param affects eligibility / quota / routing / payment, it's HMAC-signed "
     "(settings.url_signing.required + `survey-agent sign-url`) so it can't be forged."),
    ("Transport & access",
     "The runtime is served over HTTPS and the /manage console and admin API are not publicly "
     "exposed."),
]


def items():
    """[{'item': ..., 'why': ...}] — structured form for the MCP surface."""
    return [{"item": name, "why": why} for name, why in RESPONSIBILITIES]


# ---------------------------------------------------------------------------
# Runtime startup checks — run by server.serve() before it binds the socket so a
# misconfigured deployment fails loudly (or warns) instead of silently fielding
# with a guessable secret, an unreachable store, or an ephemeral disk that loses
# every response on redeploy. Pure except for an optional store-liveness ping.
# ---------------------------------------------------------------------------
import os

# The placeholder value shipped in .env.example. Never safe to field on.
DEV_PLACEHOLDER = "dev-only-change-me"

# Secrets that must never carry the dev placeholder in a real deployment.
_SECRET_VARS = (
    "SURVEY_PANEL_SHARED_SECRET", "SURVEY_WEBHOOK_SECRET", "SURVEY_URL_SECRET",
    "SURVEY_API_TOKEN", "SURVEY_SESSION_JWT_KEY",
)
# Which signing secret each fielding integration relies on — a placeholder there is fatal
# (a respondent could forge a passback), not merely advisory.
_INTEGRATION_SECRET = {"panel": "SURVEY_PANEL_SHARED_SECRET", "webhook": "SURVEY_WEBHOOK_SECRET"}

# Env markers for PaaS hosts whose filesystem is ephemeral (wiped on every redeploy), so a
# `file` store there silently loses responses. Heuristic, deliberately conservative.
_EPHEMERAL_HOST_VARS = ("RENDER", "K_SERVICE", "FLY_APP_NAME", "DYNO", "RAILWAY_ENVIRONMENT")


def _sel(env, name, default=""):
    """Like survey_agent.env_select but reads from the passed mapping — strips an inline `# comment`
    and surrounding whitespace, tolerant of a value copied straight from .env.example. Keeps
    runtime_preflight(env=...) authoritative for *every* check (not just the ephemeral-host one)."""
    return (env.get(name) or default).split("#", 1)[0].strip()


def _ping_store(store):
    """Cheap liveness probe that exercises the backend connection. Uses store.ping() if present,
    else a harmless quota read — a DB round-trip for mongo (raises if unreachable), a no-op for
    memory/file. Raises on failure."""
    p = getattr(store, "ping", None)
    if callable(p):
        return p()
    store.quota_counts("__preflight__", 0)


def runtime_preflight(env=None, store=None, integration=None, store_kind=None,
                      sign_required=False, url_secret=None, ping=True, signed_surveys=None):
    """Return a list of (level, message) for the runtime at startup.

    level ∈ {"fatal", "warn", "info"}. The caller stops the server on any "fatal" (unless the
    operator sets SURVEY_ALLOW_INSECURE=1) and prints the rest. Pure except for an optional store
    liveness ping (pass ping=False to skip, e.g. in tests).

    `sign_required` (single-survey mode) or `signed_surveys` (multi mode: the ids of served surveys
    whose definition sets settings.url_signing.required) turn on the URL-signing checks. The runtime
    fails closed per request regardless — this only surfaces the misconfiguration at startup."""
    env = env if env is not None else os.environ
    integration = (integration if integration is not None
                   else _sel(env, "SURVEY_INTEGRATION")).lower()
    store_kind = (store_kind if store_kind is not None
                  else _sel(env, "SURVEY_STORE", "memory")).lower()
    issues = []

    # --- secrets: never field on the shipped dev placeholder ---
    fielding_secret = _INTEGRATION_SECRET.get(integration)
    for name in _SECRET_VARS:
        if _sel(env, name) != DEV_PLACEHOLDER:
            continue
        if name == fielding_secret:
            issues.append(("fatal",
                f"{name} is still the dev placeholder ({DEV_PLACEHOLDER!r}) but "
                f"SURVEY_INTEGRATION={integration} signs passbacks with it — a respondent could "
                f"forge a completion. Set a strong secret before fielding."))
        else:
            issues.append(("warn",
                f"{name} is set to the dev placeholder ({DEV_PLACEHOLDER!r}); rotate it to a strong "
                f"value before production."))

    # --- url signing required but secret missing/placeholder ---
    signed_surveys = [s for s in (signed_surveys or []) if s]
    if sign_required or signed_surveys:
        us = url_secret if url_secret is not None else _sel(env, "SURVEY_URL_SECRET")
        us = (us or "").strip()
        which = (f" (survey(s): {', '.join(sorted(signed_surveys))})" if signed_surveys else "")
        if not us:
            issues.append(("warn",
                f"settings.url_signing.required is set{which} but SURVEY_URL_SECRET is not — every "
                "invitation to those surveys is REFUSED (403, fail closed) until you set the secret."))
        elif us == DEV_PLACEHOLDER:
            issues.append(("fatal",
                "SURVEY_URL_SECRET is the dev placeholder but url signing is required — signatures "
                "would be forgeable. Set a strong secret."))

    # --- store durability ---
    if store_kind == "memory":
        issues.append(("warn",
            "SURVEY_STORE=memory — responses live in process memory and are LOST on restart. Use "
            f"'file' (on a persistent disk) or a database before fielding (see {doc_ref('docs/HOSTING.md')})."))
    elif store_kind == "file" and any(env.get(k) for k in _EPHEMERAL_HOST_VARS):
        issues.append(("warn",
            "SURVEY_STORE=file on what looks like an ephemeral-filesystem host — responses are LOST "
            f"on every redeploy. Mount a persistent disk, or use a database (see {doc_ref('docs/HOSTING.md')})."))

    # --- wrong store type for the runtime hot path ---
    if store_kind == "http":
        issues.append(("fatal",
            "SURVEY_STORE=http is a read-only control-plane client and cannot back the respondent "
            "runtime (it must write sessions/quota/responses). Use file, sqlite, or a database."))
        return issues

    # --- store liveness (catches bad creds / unreachable DB before traffic) ---
    if ping and store is not None:
        try:
            _ping_store(store)
            issues.append(("info", f"store '{store_kind}' reachable."))
        except Exception as e:  # noqa: BLE001 — any backend error means "not reachable"
            networked = store_kind == "mongo" or "." in store_kind or ":" in store_kind
            issues.append(("fatal" if networked else "warn",
                f"store '{store_kind}' is not reachable: {e}. Check the SURVEY_* connection settings."))
    return issues


def checklist_lines():
    return [f"  ☐ {name} — {why}" for name, why in RESPONSIBILITIES]


def checklist_text(header=True):
    body = "\n".join(checklist_lines())
    if not header:
        return body
    return (f"Before you field to real people — you (the deployer) own these ({doc_ref(DOC)}):\n" + body)
