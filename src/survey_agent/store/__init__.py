"""Pluggable persistence for sessions, quota counters, and completed responses.

The engine is storage-agnostic; this package is a thin **seam**, not a database commitment.
survey-agent ships exactly two batteries-included backends and one reference backend — picking a
"real" database is intentionally the **deployer's** job (see "Bring your own store" below):

  memory   in-process dicts          — tests, ephemeral demo (the library default).
  file     flat files (JSON + JSONL) — the simplest persistent option: inspectable, grep-able,
                                        git/S3-syncable, zero infra. Single-PROCESS; file-locked
                                        quota increments are not safe across processes/hosts. Keeps
                                        the "everything is files" ethos: definitions AND responses
                                        are files.
  sqlite   one SQLite file           — single-box but concurrency-safe (WAL + atomic quota UPSERT),
                                        still zero-infra (pure stdlib, no server). The right step up
                                        from `file` once you run more than one worker on one box.
  mongo    MongoDB                    — a **reference** multi-HOST backend (atomic quota $inc,
                                        idempotent creation, indexed uid lookup). Treat it as a
                                        worked example of the contract, not a required dependency.

Bring your own store: set SURVEY_STORE to an import path — "my_pkg.store:PostgresStore" (or
"my_pkg.store.PostgresStore") — and get_store() will import and instantiate it (kwargs/env passed
through). Implement the interface below against Postgres / DynamoDB / Redis / Durable Objects /
your warehouse; survey-agent stays unopinionated. We deliberately do **not** ship those adapters.

Store interface (all backends implement):
  get_or_create_session(survey_id, rid, uid, factory) -> (session, created_bool)
  save_session(session)
  get_session_by_id(session_id) -> session | None
  quota_counts(survey_id, version) -> {quota_id: int}
  incr_quota(survey_id, version, quota_id) -> int           # new count (atomic per backend)
  record_response(survey_id, response)                      # append-only export record
  list_responses(survey_id=None, uid=None) -> [response]
"""
import os

BUILTINS = ("memory", "file", "sqlite", "mongo", "http")


def _load_custom(path, **kwargs):
    """Import a deployer-supplied store from 'module:Class' or 'module.Class' and instantiate it."""
    import importlib
    mod, _, cls = path.replace(":", ".").rpartition(".")
    if not mod:
        raise ValueError(f"SURVEY_STORE={path!r} is not an import path (use 'pkg.module:ClassName')")
    Store = getattr(importlib.import_module(mod), cls)
    return Store(**kwargs)


def get_store(kind=None, **kwargs):
    from survey_agent import env_select
    kind = (kind or env_select("SURVEY_STORE", "memory")) or "memory"
    low = kind.lower()
    if low == "memory":
        from .memory import MemoryStore
        return MemoryStore()
    if low == "file":
        from .file import FileStore
        return FileStore(kwargs.get("data_dir") or os.environ.get("SURVEY_DATA_DIR", "./survey_data"))
    if low == "sqlite":
        from .sqlite import SQLiteStore
        return SQLiteStore(kwargs.get("path") or os.environ.get("SURVEY_SQLITE_PATH"))
    if low == "mongo":
        from .mongo import MongoStore
        return MongoStore(
            uri=kwargs.get("uri") or os.environ.get("SURVEY_MONGODB_URI", "mongodb://localhost:27017"),
            db=kwargs.get("db") or os.environ.get("SURVEY_MONGODB_DB", "survey_agent"),
        )
    if low == "http":                        # read responses through the API gateway (control plane)
        from .http import HTTPStore
        return HTTPStore(base=kwargs.get("base"), token=kwargs.get("token"))
    if "." in kind or ":" in kind:           # deployer-supplied backend (bring your own store)
        return _load_custom(kind, **kwargs)
    raise ValueError(
        f"unknown SURVEY_STORE: {kind!r}. Use a built-in ({'|'.join(BUILTINS)}) or an import "
        f"path to your own store, e.g. 'my_pkg.store:PostgresStore'.")
