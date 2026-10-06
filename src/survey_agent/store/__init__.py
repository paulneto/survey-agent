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
  list_responses(survey_id=None, uid=None, id_fields=None) -> [response]
  reserve_quotas(survey_id, version, limits) -> bool         # all-or-nothing capacity reservation
  find_respondent(respondent_id, id_fields=("uid",), survey_id=None) -> {responses, sessions}
  erase_respondent(respondent_id, id_fields=("uid",), survey_id=None)
      -> {responses: n, sessions: m, upload_refs: [ref], webhook_keys: [key]}
  delete_responses(uid=None, survey_id=None) -> n            # compat wrapper over erase_respondent

Quota reservation: `limits` is {quota_id: limit_or_None}. The store increments EVERY listed counter
iff each limited counter is currently below its limit — atomically, all-or-nothing (no partial
increments). Returns True when reserved, False when any limited bucket is full. `incr_quota` stays as
the unconditional single-counter primitive.

Respondent erasure (DSAR): a respondent is matched by value against any of `id_fields` — the url
variable names that carry a person's id (`uid` by default; surveys can rename it with
`settings.identity.respondent` and a deployment with SURVEY_RESPONDENT_VAR, so callers pass the union).
Responses match on `url_variables[f]`, sessions on `url_vars[f]` — every survey, partial sessions and
retakes included. The store deletes what it holds and *returns* the upload refs (blob references found
in the deleted answers) and webhook idempotency keys (for clearing queued deliveries); deleting the
blobs and queue items is the caller's job, since those live in other seams.
"""
import hashlib
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


# ---- shared helpers for the respondent-erasure contract (used by every built-in store) ----

def norm_id_fields(id_fields):
    """A de-duplicated tuple of identity url-var names (default ("uid",))."""
    if id_fields is None:
        return ("uid",)
    if isinstance(id_fields, str):
        id_fields = (id_fields,)
    out = []
    for f in id_fields:
        if f and str(f) not in out:
            out.append(str(f))
    return tuple(out) or ("uid",)


def respondent_matches(url_vars, respondent_id, id_fields):
    """True if any of `id_fields` in this url-var mapping equals `respondent_id` (compared as str)."""
    uv = url_vars or {}
    rid = str(respondent_id)
    return any(uv.get(f) is not None and str(uv.get(f)) == rid for f in id_fields)


def response_matches(response, respondent_id, id_fields):
    return respondent_matches(response.get("url_variables"), respondent_id, id_fields)


def session_matches(session, respondent_id, id_fields):
    return respondent_matches(session.get("url_vars"), respondent_id, id_fields)


def upload_refs_in(obj, out=None):
    """Every upload reference ({backend, key, ...}, see store/uploads.py) nested anywhere in `obj`."""
    out = [] if out is None else out
    if isinstance(obj, dict):
        if isinstance(obj.get("key"), str) and isinstance(obj.get("backend"), str):
            out.append(obj)
        else:
            for v in obj.values():
                upload_refs_in(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            upload_refs_in(v, out)
    return out


def record_upload_refs(record):
    """Upload refs in a session (`answers`) or a response (`survey_data`)."""
    return upload_refs_in([record.get("answers"), record.get("survey_data")])


def session_webhook_keys(session):
    """Idempotency keys of the webhook actions this session fired — the same derivation as
    engine.runtime.SurveyRuntime._collect_webhooks (sha256 of "<session_id>|webhook:<index>"), so a
    caller can find any queued (outbox) delivery that carries this respondent's data."""
    sid = session.get("session_id")
    if not sid:
        return []
    return [hashlib.sha256(f"{sid}|webhook:{i}".encode()).hexdigest()
            for i in (session.get("fired_webhooks") or [])]


def erasure_result(responses, sessions):
    """Build the erase_respondent() return value from the deleted response + session records."""
    refs, seen = [], set()
    for rec in list(responses) + list(sessions):
        for ref in record_upload_refs(rec):
            k = (ref.get("backend"), ref.get("key"))
            if k not in seen:
                seen.add(k)
                refs.append(ref)
    keys = []
    for s in sessions:
        for k in session_webhook_keys(s):
            if k not in keys:
                keys.append(k)
    return {"responses": len(responses), "sessions": len(sessions), "upload_refs": refs,
            "webhook_keys": keys}


def check_reservation(counts, limits):
    """True iff every limited bucket in `limits` is below its limit given current `counts`."""
    for qid, lim in (limits or {}).items():
        if lim is not None and counts.get(qid, 0) >= lim:
            return False
    return True
