"""Where is this runtime publicly reachable? — one resolver, used by every surface.

The drop-in `survey_url` is the product's whole contract with the outside world, and getting its
base wrong is the most expensive silent mistake available: a `http://localhost:8000/...` link pasted
into a panel or an email blast reaches nobody, and you only find out when the completes never arrive.

Resolution order (first hit wins):

  1. an explicit override            — `--base` / the `base_url` argument
  2. SURVEY_PUBLIC_BASE_URL          — the operator said so; always authoritative
  3. the PaaS host's own env var     — Render/Railway/Fly/Vercel tell us our public URL for free,
                                       so a one-click deploy needs no manual follow-up step
  4. http://localhost:8000           — the local-dev default

Step 3 is what makes "click Deploy, get a working survey_url" possible. Every host below injects its
public hostname into the container environment; we just read it.

Env-reading only — no network, no imports beyond the stdlib. NOT imported by `engine/` (which stays
pure); the CLI, runtime, console and MCP surfaces all call in here.
"""
import os

LOCAL_DEFAULT = "http://localhost:8000"

# (env var, how to turn its value into a base URL). Ordered — Render first because its variable is
# already a full URL and is the blueprint this repo ships.
_HOST_URL_VARS = (
    ("RENDER_EXTERNAL_URL",    lambda v: v),                        # Render: full https:// URL
    ("RAILWAY_PUBLIC_DOMAIN",  lambda v: f"https://{v}"),           # Railway: bare domain
    ("VERCEL_PROJECT_PRODUCTION_URL", lambda v: f"https://{v}"),    # Vercel: bare domain
    ("VERCEL_URL",             lambda v: f"https://{v}"),           # Vercel (per-deployment)
    ("FLY_APP_NAME",           lambda v: f"https://{v}.fly.dev"),   # Fly: derive from the app name
    ("HEROKU_APP_NAME",        lambda v: f"https://{v}.herokuapp.com"),
)

# Friendly names for `doctor` output, so an operator can see *why* a base was chosen.
_HOST_LABELS = {
    "RENDER_EXTERNAL_URL": "Render", "RAILWAY_PUBLIC_DOMAIN": "Railway",
    "VERCEL_PROJECT_PRODUCTION_URL": "Vercel", "VERCEL_URL": "Vercel",
    "FLY_APP_NAME": "Fly.io", "HEROKU_APP_NAME": "Heroku",
}

_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]")


def _clean(v):
    """Tolerate a value copied out of .env.example (inline `# comment`, stray quotes/whitespace)."""
    if not v:
        return ""
    v = v.split("#", 1)[0].strip().strip('"').strip("'")
    return v.rstrip("/")


def detect_host(env=None):
    """(label, base_url) for the PaaS we appear to be running on, or (None, None).

    Pure env inspection — used both to resolve the base and to explain the choice in `doctor`."""
    env = env if env is not None else os.environ
    for var, to_url in _HOST_URL_VARS:
        val = _clean(env.get(var))
        if val:
            return _HOST_LABELS.get(var, var), _clean(to_url(val))
    return None, None


def public_base_url(override=None, env=None, default=LOCAL_DEFAULT):
    """The base URL to build drop-in survey links from. See the module docstring for the order."""
    env = env if env is not None else os.environ
    explicit = _clean(override)
    if explicit:
        return explicit
    configured = _clean(env.get("SURVEY_PUBLIC_BASE_URL"))
    if configured:
        return configured
    _, detected = detect_host(env)
    if detected:
        return detected
    return _clean(default) or LOCAL_DEFAULT


def base_url_source(override=None, env=None):
    """Where the resolved base came from: 'explicit' | 'SURVEY_PUBLIC_BASE_URL' | '<Host>' | 'default'.
    Surfaced by `doctor` so a wrong URL is traceable to the thing that set it."""
    env = env if env is not None else os.environ
    if _clean(override):
        return "explicit"
    if _clean(env.get("SURVEY_PUBLIC_BASE_URL")):
        return "SURVEY_PUBLIC_BASE_URL"
    label, _ = detect_host(env)
    return label or "default"


def is_local(base):
    """True if this base URL is only reachable from the machine that served it.

    The check that stops a localhost link being handed to a panel. Substring-free — matches on the
    parsed hostname so `https://localhost.example.com` is correctly treated as public."""
    from urllib.parse import urlsplit
    host = (urlsplit(base or "").hostname or "").lower()
    return host in _LOCAL_HOSTS or host.endswith(".local")


def unreachable_warning(base):
    """A ready-to-print warning when `base` can't be reached by respondents, else None."""
    if not is_local(base):
        return None
    from survey_agent.preflight import doc_ref
    return (f"This survey_url points at {base} — reachable only from this machine, so a respondent "
            f"(or a panel) cannot open it. Deploy the runtime and set SURVEY_PUBLIC_BASE_URL (a "
            f"one-click Render/Fly/Railway deploy sets it for you — see {doc_ref('docs/HOSTING.md')}), or pass "
            f"--base https://your-host.example.com.")
