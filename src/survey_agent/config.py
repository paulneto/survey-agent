"""Operator configuration for the CLI / runtime / console / MCP entry points.

One place decides the effective deployment defaults, so every surface agrees on where responses live
and how passbacks are delivered. Precedence, highest first:

  1. the environment (including values a local ``.env`` filled in — a real export always wins over .env)
  2. ``survey-agent.config.yaml`` (``$SURVEY_CONFIG``, else ``./survey-agent.config.yaml``)
  3. the built-in default (``SURVEY_STORE=file``, ``SURVEY_INTEGRATION=none``)

``apply_defaults()`` writes the resolved values into ``os.environ`` (so every module that reads env —
the runtime, report/export, the console, the MCP tools — sees the same answer) and records where each
came from for ``survey-agent doctor``. It is called by the entry points only; the library itself (and
the in-memory simulator / tests that build their own runtimes) is unaffected.

Never imported by the pure engine. Stdlib + PyYAML (already a core dependency).
"""
import os

# Built-in defaults for an unset selector. `file` persists responses under ./survey_data so `report`,
# `export` and `dsar` see what `serve`/`demo` collected; `none` means no outbound passback unless the
# operator configures one.
DEFAULTS = {"SURVEY_STORE": "file", "SURVEY_INTEGRATION": "none"}

# env var -> (config section, key inside a mapping form). Both `store: file` and `store: {backend: file}`
# are accepted, likewise `theme: paper` / `theme: {default: paper}`.
CONFIG_KEYS = {
    "SURVEY_STORE": ("store", "backend"),
    "SURVEY_INTEGRATION": ("integration", "backend"),
    "SURVEY_THEME": ("theme", "default"),
    "SURVEY_DIR": ("surveys", "path"),
}

# var -> (source label, value) for doctor; filled by apply_defaults().
SOURCES = {}
_APPLIED = {}          # var -> the value apply_defaults() itself wrote (so a re-run keeps the true source)


def config_path():
    """The operator config file in effect, or None."""
    for p in (os.environ.get("SURVEY_CONFIG"), "survey-agent.config.yaml"):
        if p and os.path.exists(p):
            return p
    return None


def load_config():
    """The parsed operator config (a dict), {} if absent or unreadable."""
    p = config_path()
    if not p:
        return {}
    try:
        import yaml
        with open(p) as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _config_value(cfg, section, key):
    v = cfg.get(section)
    if isinstance(v, dict):
        v = v.get(key)
    if v is None or isinstance(v, (dict, list)):
        return None
    v = str(v).strip()
    return v or None


def apply_defaults(environ=None):
    """Resolve each selector (env > config yaml > built-in default), write it into the environment, and
    record its source. Idempotent. Returns SOURCES."""
    env = os.environ if environ is None else environ
    from survey_agent import DOTENV_KEYS
    cfg = load_config()
    cpath = config_path()
    for var, (section, key) in CONFIG_KEYS.items():
        cur = (env.get(var) or "").split("#", 1)[0].strip() or None
        if cur is not None and _APPLIED.get(var) == env.get(var):
            cur = None                                     # we set it on an earlier call — re-resolve
        if cur is not None:
            src = f".env ({DOTENV_KEYS[var]})" if var in DOTENV_KEYS else "environment"
            SOURCES[var] = (src, cur)
            continue
        v = _config_value(cfg, section, key)
        if v is not None:
            env[var] = v
            _APPLIED[var] = v
            SOURCES[var] = (f"config ({cpath})", v)
        elif var in DEFAULTS:
            env[var] = DEFAULTS[var]
            _APPLIED[var] = DEFAULTS[var]
            SOURCES[var] = ("default", DEFAULTS[var])
        else:
            SOURCES[var] = ("unset", None)
    return SOURCES


def source_of(var):
    """(source label, value) for a selector — resolving now if apply_defaults() hasn't run."""
    if var not in SOURCES:
        apply_defaults()
    return SOURCES.get(var, ("unset", None))


# ----------------------------------------------------------------------------- launch gate
def launch_token():
    return os.environ.get("SURVEY_LAUNCH_APPROVAL_TOKEN") or None


_APPROVED = None


def approved_launch_token():
    """The launch approval this process has already verified (None if none) — so a CLI launch against
    an HTTP definition source can prove the approval to the gateway, which enforces the same gate."""
    return _APPROVED


def may_mark_live(approve=None, surface="cli"):
    """(allowed, reason). The one launch-gate rule every surface applies before marking a survey live.

    With SURVEY_LAUNCH_APPROVAL_TOKEN set, every surface needs the matching token. With it unset, a
    local operator (CLI / console) may mark live, but the remote surfaces — the MCP connector and the
    API gateway — may not (fail-closed: fielding to real people is a human act)."""
    import hmac
    tok = launch_token()
    if tok:
        if approve and hmac.compare_digest(str(approve), tok):
            global _APPROVED
            _APPROVED = str(approve)      # forwarded by remote definition stores (definitions/http.py)
            return True, None
        return False, ("launch is gated here (SURVEY_LAUNCH_APPROVAL_TOKEN is set) — a human must supply "
                       "the approval token to mark a survey live")
    if surface in ("mcp", "api"):
        return False, ("no SURVEY_LAUNCH_APPROVAL_TOKEN is configured, so marking live is impossible via "
                       f"the {'connector' if surface == 'mcp' else 'API gateway'} — a human marks it live "
                       "from the CLI or console")
    return True, None
