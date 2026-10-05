"""survey-agent — the agentic survey engine."""
import os


def _pkg_version():
    """The installed distribution's version — pyproject.toml is the single source of truth."""
    try:
        from importlib.metadata import version, PackageNotFoundError
        try:
            return version("survey-agent")
        except PackageNotFoundError:
            return "0+unknown"
    except Exception:  # pragma: no cover
        return "0+unknown"


__version__ = _pkg_version()

# Keys that load_dotenv() actually filled from a .env file (for `doctor`'s "where did this come from").
DOTENV_KEYS = {}


def env_select(name, default=""):
    """Read a short selector env var (SURVEY_STORE / SURVEY_PERSONAS / …) robustly: strip surrounding
    whitespace and any inline ``# comment``. Makes the seams tolerant of a value copied straight from
    ``.env.example`` or written with a trailing comment (which some env loaders/injectors don't strip)."""
    return (os.environ.get(name) or default).split("#", 1)[0].strip()


def _env_unset(value):
    """True if an env var is effectively *not set* for our purposes: missing, empty, or a literal
    unexpanded ``${VAR}`` placeholder. The last case matters for MCP configs — a client may pass
    ``"ANTHROPIC_API_KEY": "${ANTHROPIC_API_KEY}"`` straight through (or expand it to "") when the
    shell var isn't set, and that junk value must NOT block ``.env`` from supplying the real one."""
    return value is None or value == "" or (value.startswith("${") and value.endswith("}"))


def load_dotenv(path=".env"):
    """Minimal, dependency-free ``.env`` loader: ``KEY=VALUE`` lines → ``os.environ``, filling only
    keys that are unset (see ``_env_unset``) so a real exported value always wins. Shared by the CLI
    and the MCP server so both pick up ANTHROPIC_API_KEY / SURVEY_* from a local ``.env`` without
    anyone exporting by hand. Returns the path if it was read, else None."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if k.startswith("export "):
                    k = k[len("export "):].strip()
                if v[:1] in ('"', "'"):
                    v = v[1:].split(v[0], 1)[0]                      # quoted value → its contents
                elif v.startswith("#"):
                    v = ""                                           # `KEY=   # comment` → empty, not the comment
                else:
                    v = v.split(" #", 1)[0].split("\t#", 1)[0].rstrip()   # drop an inline comment
                if k and _env_unset(os.environ.get(k)):
                    os.environ[k] = v
                    DOTENV_KEYS[k] = path
        return path
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError):
        return None


def load_env_files():
    """Find and load the right ``.env`` for a process that may not be launched from the project dir
    (e.g. an MCP server Claude Code spawns from elsewhere). Tries, in order, the first that exists:
    ``$SURVEY_ENV_FILE``, ``./.env``, and ``<parent of $SURVEY_DIR>/.env`` — so pointing SURVEY_DIR at
    ``…/project/surveys`` also picks up ``…/project/.env``. setdefault semantics: earlier wins, and an
    already-exported var is never clobbered."""
    candidates = []
    if os.environ.get("SURVEY_ENV_FILE"):
        candidates.append(os.environ["SURVEY_ENV_FILE"])
    candidates.append(".env")
    sd = os.environ.get("SURVEY_DIR")
    if sd:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(sd)), ".env"))
    loaded = []
    for c in candidates:
        if load_dotenv(c):
            loaded.append(c)
    return loaded
