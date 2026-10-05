"""Definition store — where survey definitions + lifecycle are read from and published to.

A **backend-agnostic seam** (the engine imports no specific store), selected by
`SURVEY_DEFINITION_SOURCE`:

  file   definitions = `.survey.json` in SURVEY_DIR, lifecycle = `.registry.json` sidecar (the
         DEFAULT — today's behavior; publish = your `deploy.command` / git push → redeploy).
  mongo  a **reference** runtime backend: definitions + lifecycle in a DB, so publish + pause/close
         are store writes (instantly live, no redeploy) and version-pinning is real. Not a required
         dependency — like `SURVEY_STORE=mongo`.

Bring your own: set SURVEY_DEFINITION_SOURCE to an import path ("my_pkg.defs:PostgresDefinitions");
implement the `DefinitionStore` interface (definitions/base.py). The core stays unopinionated.
"""
import os

BUILTINS = ("file", "mongo", "http")


def _load_custom(path, **kwargs):
    import importlib
    mod, _, cls = path.replace(":", ".").rpartition(".")
    if not mod:
        raise ValueError(f"SURVEY_DEFINITION_SOURCE={path!r} is not an import path (use 'pkg.module:ClassName')")
    return getattr(importlib.import_module(mod), cls)(**kwargs)


def get_definition_store(kind=None, **kwargs):
    from survey_agent import env_select
    kind = (kind or env_select("SURVEY_DEFINITION_SOURCE", "file")) or "file"
    low = kind.lower()
    if low == "file":
        from .file import FileDefinitionStore
        return FileDefinitionStore(kwargs.get("dir") or os.environ.get("SURVEY_DIR") or "surveys")
    if low == "mongo":
        from .mongo import MongoDefinitionStore
        return MongoDefinitionStore(
            uri=kwargs.get("uri") or os.environ.get("SURVEY_MONGODB_URI", "mongodb://localhost:27017"),
            db=kwargs.get("db") or os.environ.get("SURVEY_MONGODB_DB", "survey_agent"),
        )
    if low == "http":                       # talk to the API gateway instead of a direct DB
        from .http import HTTPDefinitionStore
        return HTTPDefinitionStore(base=kwargs.get("base"), token=kwargs.get("token"))
    if "." in kind or ":" in kind:          # deployer-supplied backend (bring your own)
        return _load_custom(kind, **kwargs)
    raise ValueError(
        f"unknown SURVEY_DEFINITION_SOURCE: {kind!r}. Use a built-in ({'|'.join(BUILTINS)}) or an "
        f"import path to your own, e.g. 'my_pkg.defs:PostgresDefinitions'.")
