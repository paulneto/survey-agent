"""Behavioral enrichment providers + survey resolution.

A survey declares the behavioral facts it needs in `settings.enrichments` (inline specs, or
string ids resolved from the flat-file catalog in ./enrichments/). At session START — i.e. once
a respondent *accepts and begins* (not at offer time, so we don't query for non-starters) — the
runtime calls `prefetch_for_survey()` to warm the data while the intro page renders, then reads
it with `resolve_for_survey()` (cache-first, fast). Values land on session["enrichments"] and are
usable in piping ({{behavior:id}}), logic (ref.kind=="enrichment"), and routing.

Providers (SURVEY_ENRICH env: static | firebolt | your_pkg.module:YourProvider):
  StaticProvider    evaluates specs in-Python over fixture events — demo/tests, instant
  FireboltProvider  reference warehouse adapter: compiles each spec to one tight query
                    (firebolt_sql), short timeout, cache-first with write-back; prefetch warms the
                    cache asynchronously
  <import path>     any class implementing EnrichmentProvider (`resolve(uid, specs) -> {id: value}`,
                    optional `prefetch`) — bring your own warehouse/behavioral DB without forking
"""
import json
import os
import threading
from datetime import datetime, timedelta, timezone

from . import spec as spec_mod

CATALOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "enrichments")


# ----------------------------------------------------------------- catalog
def load_catalog(directory=CATALOG_DIR):
    catalog = {}
    if not os.path.isdir(directory):
        return catalog
    for name in os.listdir(directory):
        if name.endswith(".json") and not name.startswith("_"):
            with open(os.path.join(directory, name)) as f:
                s = json.load(f)
                catalog[s["id"]] = s
    return catalog


def _all_specs(survey_def, catalog=None):
    catalog = catalog if catalog is not None else load_catalog()
    out = []
    for item in survey_def.get("settings", {}).get("enrichments", []):
        if isinstance(item, dict):
            out.append(item)
        elif item in catalog:
            out.append(catalog[item])
    return out


def _specs_for_survey(survey_def, catalog=None):
    # behavioral/start-phase specs only (handled by the configured store/Firebolt provider).
    # `source: llm` specs are resolved separately at completion (resolve_llm_for_session).
    return [s for s in _all_specs(survey_def, catalog) if s.get("source") != "llm"]


# ----------------------------------------------------------------- providers
class EnrichmentProvider:
    def resolve(self, uid, specs):
        raise NotImplementedError

    def prefetch(self, uid, specs):
        """Warm whatever cache backs resolve(). Default: do the work and discard."""
        try:
            self.resolve(uid, specs)
        except Exception:
            pass


class StaticProvider(EnrichmentProvider):
    """Evaluates specs in-Python over fixture events (stands in for a warehouse in demo/tests).

    Two modes:
      • global  — `events_by_type` (or _demo_events.json): every uid sees the same events.
      • per-uid — `by_uid` ({uid: {event_type: [events]}, "_default": {...}}): each respondent
                  has their *own* behavior, so per-user gating actually triggers (some respondents have
                  a streaming search and get the "why" page; others don't and skip it). This mirrors
                  what the FireboltProvider does with `WHERE respondent_id = :uid`.
    Each event may use `days_ago` (converted to a recent timestamp) so fixtures stay stable.
    """

    def __init__(self, events_by_type=None, by_uid=None, fixtures_path=None):
        self.by_uid = by_uid
        if events_by_type is None and by_uid is None:
            path = fixtures_path or os.path.join(CATALOG_DIR, "_demo_events.json")
            events_by_type = json.load(open(path)) if os.path.exists(path) else {}
        self.events_by_type = events_by_type or {}

    def _events_for(self, uid, event_type, now):
        if self.by_uid is not None:
            src = self.by_uid.get(str(uid)) or self.by_uid.get("_default") or {}
        else:
            src = self.events_by_type
        out = []
        for e in src.get(event_type, []):
            e = dict(e)
            if "days_ago" in e:
                e["event_time"] = (now - timedelta(days=e.pop("days_ago"))).isoformat()
            out.append(e)
        return out

    def resolve(self, uid, specs):
        now = datetime.now(timezone.utc)
        return {s["id"]: spec_mod.evaluate(s, self._events_for(uid, s["event_type"], now), now)
                for s in specs}


class FireboltProvider(EnrichmentProvider):
    """Production path: one compiled query per spec, short timeout, cache-first with write-back.
    firebolt-sdk is imported lazily; cache is any object with get/set(key, value, ttl)."""

    def __init__(self, connect_kwargs=None, cache=None, timeout_seconds=2.0):
        self.connect_kwargs = connect_kwargs or {}
        self.cache = cache
        self.timeout = timeout_seconds

    def _cache_key(self, uid, s):
        return f"enrich:{uid}:{s['id']}:{s.get('event_type')}"

    def resolve(self, uid, specs):
        from .firebolt_sql import compile_value_sql
        out = {}
        to_query = []
        for s in specs:
            if self.cache:
                hit = self.cache.get(self._cache_key(uid, s))
                if hit is not None:
                    out[s["id"]] = hit
                    continue
            to_query.append(s)
        if to_query:
            conn = self._connect()  # lazy
            try:
                cur = conn.cursor()
                for s in to_query:
                    sql, params = compile_value_sql(s, uid)
                    cur.execute(sql, params)
                    row = cur.fetchone()
                    val = row[0] if row else None
                    out[s["id"]] = val
                    if self.cache:
                        self.cache.set(self._cache_key(uid, s), val, s.get("ttl_seconds", 3600))
            finally:
                conn.close()
        return out

    def prefetch(self, uid, specs):
        threading.Thread(target=self.resolve, args=(uid, specs), daemon=True).start()

    def _connect(self):  # pragma: no cover - requires firebolt-sdk + creds
        from firebolt.db import connect
        from firebolt.client.auth import ClientCredentials
        return connect(auth=ClientCredentials(**self.connect_kwargs.get("auth", {})),
                       **{k: v for k, v in self.connect_kwargs.items() if k != "auth"})


def _load_custom(path, **kwargs):
    """Import a deployer-supplied provider from 'module:Class' or 'module.Class' and instantiate it."""
    import importlib
    mod, _, cls = path.replace(":", ".").rpartition(".")
    if not mod or not cls:
        raise ValueError(f"unknown SURVEY_ENRICH: {path!r} (built-ins: static|firebolt, "
                         f"or an import path 'pkg.module:ProviderClass')")
    Provider = getattr(importlib.import_module(mod), cls)
    return Provider(**kwargs)


def get_provider(kind=None, **kwargs):
    from survey_agent import env_select
    kind = (kind or env_select("SURVEY_ENRICH", "static")) or "static"
    low = kind.lower()
    if low == "static":
        return StaticProvider(**kwargs)
    if low == "firebolt":
        return FireboltProvider(**kwargs)
    return _load_custom(kind, **kwargs)


# ----------------------------------------------------------------- survey helpers
def prefetch_for_survey(survey_def, uid, provider, catalog=None):
    specs = _specs_for_survey(survey_def, catalog)
    if specs:
        provider.prefetch(uid, specs)


def resolve_for_survey(survey_def, uid, provider, catalog=None):
    specs = _specs_for_survey(survey_def, catalog)
    return provider.resolve(uid, specs) if specs else {}


# ----------------------------------------------------------------- LLM context (opt-in + audited)
def _llm_enabled():
    return os.environ.get("SURVEY_LLM_ENRICH", "").lower() in ("1", "true", "yes")


def _gather_inputs(input_ids, session):
    answers = session.get("answers", {})
    if input_ids in (["all_answers"], "all_answers"):
        return {k: v for k, v in answers.items() if v not in (None, "")}
    return {i: answers.get(i) for i in (input_ids or [])}


def resolve_llm_for_session(survey_def, session, llm=None, enabled=None, now=None):
    """Resolve `source: llm` context at completion — sending respondent data to an LLM.

    FAIL-CLOSED + AUDITED: runs only if the deployment enables it (SURVEY_LLM_ENRICH) AND the spec is
    explicitly `opt_in: true`, plus optional respondent consent (`requires_consent`: a question id
    whose answer must be truthy). EVERY call is recorded in the returned audit list (model, fields
    sent, status) — the caller persists it on the response. Returns (values, audit_records)."""
    specs = [s for s in _all_specs(survey_def) if isinstance(s, dict) and s.get("source") == "llm"]
    if not specs:
        return {}, []
    enabled = _llm_enabled() if enabled is None else enabled
    import survey_agent.ai as ai
    llm = llm or ai.get_llm()
    values, audit = {}, []
    for s in specs:
        rec = {"id": s["id"], "inputs": s.get("inputs"), "model": getattr(llm, "name", "?"),
               "at": now, "consent": "opt_in"}
        if not (enabled and s.get("opt_in") is True):
            rec["status"] = "skipped_not_opted_in"
            audit.append(rec)
            continue
        consent_q = s.get("requires_consent")
        if consent_q and str(session.get("answers", {}).get(consent_q, "")).lower() not in ("yes", "true", "1"):
            rec["status"] = "skipped_no_respondent_consent"
            audit.append(rec)
            continue
        inputs = _gather_inputs(s.get("inputs"), session)
        try:
            values[s["id"]] = llm.analyze(s.get("task") or s.get("prompt") or "Summarize.", inputs, s.get("output"))
            rec["status"] = "ok"
        except Exception:
            values[s["id"]] = None      # fail-open on the value (never breaks the survey)
            rec["status"] = "error"
        audit.append(rec)
    return values, audit
