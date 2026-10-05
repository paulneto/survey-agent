"""Resolving the docs a pip-installed user (or agent) is pointed at.

Runtime messages and docstrings say things like "see docs/HOSTING.md". In a source checkout that path
resolves. From `pip install survey-agent` it does not — there is no checkout — so every one of those
pointers is a dead end for exactly the audience that most needs them.

The docs are NOT bundled into the wheel on purpose: getting them there requires either moving the
canonical `docs/` under `src/survey_agent/` (a reorg that buys little) or copying them at build time
(two copies, guaranteed to drift). Instead this module resolves a doc name to whatever is actually
reachable — the local file when running from a checkout, else the canonical URL on the repo.

Stdlib only.
"""
import os

REPO = "https://github.com/paulneto/survey-agent"
BRANCH = "main"
RAW = f"{REPO}/blob/{BRANCH}"

# The doc map. Keys are the short names `survey-agent docs <name>` accepts; values are (repo-relative
# path, one-line purpose). test_docs.py checks every entry exists and that docs/README.md links it.
DOCS = {
    "agents": ("AGENTS.md", "LLM/agent entry point: the loop, the CLI, the self-correction signals"),
    "readme": ("README.md", "what survey-agent is, the agentic loop, quickstart"),
    "quickstart": ("docs/QUICKSTART.md", "install → a running survey, step by step"),
    "user-guide": ("docs/USER_GUIDE.md", "operating surveys day to day"),
    "logic": ("docs/LOGIC_IS_DATA.md", "the core design decision: conditions are a bounded JSON AST"),
    "engineering": ("docs/ENGINEERING.md", "architecture, the pure engine, and the swappable seams"),
    "hosting": ("docs/HOSTING.md", "what your deployment needs; hosting options"),
    "deploy": ("docs/DEPLOY.md", "shared team deployment walkthrough + command cheat sheet"),
    "hosts": ("docs/AGENT_HOSTS.md", "which agent host (Claude Code / Cowork / ChatGPT / Codex) can do what"),
    "console": ("docs/CONSOLE.md", "the operator console and the survey lifecycle"),
    "responsibilities": ("docs/OPERATOR_RESPONSIBILITIES.md", "what the operator owns before fielding"),
    "security": ("docs/DATA_SECURITY.md", "data handling: what the engine does, what the deployer owns"),
    "primitive": ("docs/PRIMITIVE.md", "begin_survey/answer_survey — structured elicitation from a human"),
    "coverage": ("docs/FEATURE_COVERAGE.md", "capability matrix: supported, partial, not supported, out of scope"),
    "personas": ("docs/PERSONAS.md", "persona pre-testing"),
    "modes": ("docs/AGENTIC_MODES.md", "the agentic modes the engine supports"),
    "walkthrough": ("docs/SKILL_WALKTHROUGH.md", "build and test a first survey with the skills, step by step"),
    "decisions": ("docs/dev/DESIGN_DECISIONS.md", "settled design decisions and their rationale"),
    "templates": ("docs/dev/TEMPLATES.md", "the template / scaffolding seam"),
    "contributing": ("CONTRIBUTING.md", "dev setup, architecture rules, how to add things"),
    "changelog": ("CHANGELOG.md", "release notes"),
    "docs-index": ("docs/README.md", "the documentation map"),
}


def _repo_root():
    """The checkout root if we're running from one, else None.

    Walks up from this file: in a source checkout (editable or `pip install .` from a clone) the repo
    root is two levels above the package; in site-packages it isn't, and there's no `docs/` there."""
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (os.path.dirname(os.path.dirname(here)), os.getcwd()):
        if os.path.isdir(os.path.join(candidate, "docs")) and \
           os.path.isfile(os.path.join(candidate, "AGENTS.md")):
            return candidate
    return None


def url(name_or_path):
    """The canonical web URL for a doc, by short name or repo-relative path. Always resolvable."""
    path = DOCS.get(name_or_path, (name_or_path, ""))[0]
    return f"{RAW}/{path}"


def resolve(name_or_path):
    """(kind, location) where kind is 'file' or 'url'.

    Prefers a real local file so an offline checkout keeps working; falls back to the URL, which is
    what a pip-installed agent gets (and can fetch)."""
    path = DOCS.get(name_or_path, (name_or_path, ""))[0]
    root = _repo_root()
    if root:
        local = os.path.join(root, path)
        if os.path.isfile(local):
            return "file", local
    return "url", f"{RAW}/{path}"


def read(name_or_path):
    """The doc's text if it's local, else None (caller shows the URL)."""
    kind, loc = resolve(name_or_path)
    if kind != "file":
        return None
    with open(loc, encoding="utf-8") as fh:
        return fh.read()


def index_lines():
    """The doc map as printable lines, each resolving to a file path or a URL."""
    width = max(len(k) for k in DOCS)
    out = []
    for name, (path, purpose) in DOCS.items():
        kind, loc = resolve(name)
        marker = " " if kind == "file" else "↗"
        out.append(f"  {marker} {name.ljust(width)}  {purpose}\n    {' ' * width}   {loc}")
    return out
