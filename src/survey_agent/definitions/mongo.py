"""mongo DefinitionStore — a **reference** runtime backend (not a required dependency; like
`SURVEY_STORE=mongo`). Definitions live in `survey_definitions` keyed `(survey_id, version)`,
immutable; lifecycle in `survey_registry` keyed `survey_id`, mutable. `put_definition` auto-bumps the
version on a content-hash change (identical content is a no-op). Serving reads the registry's
`live_version`; in-flight sessions keep their pinned `(id, version)`. `put_definition` never touches
the registry — only an approved mark-live (`set_registry`) moves `live_version`; `get_registry` reports
the newest authored version as `latest_version`.

The engine never imports this — it's reached only through `get_definition_store()`.
"""
import hashlib
import json

from survey_agent import registry as _registry          # only for now()/derived_state helpers (pure stdlib)
from .base import DefinitionStore, check_survey_id, marks_live, valid_survey_id


def _hash(spec):
    """Content identity, excluding the auto-managed `version` field — so the version number itself
    never triggers a bump, and a re-imported (renumbered) survey reconciles clean against its file."""
    s = json.loads(json.dumps(spec))
    s.get("survey", {}).pop("version", None)
    return hashlib.sha256(json.dumps(s, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class MongoDefinitionStore(DefinitionStore):
    def __init__(self, definitions=None, registry=None, uri=None, db=None):
        # collections injectable for tests; else connect via pymongo (the [mongo] extra).
        if definitions is not None and registry is not None:
            self._defs, self._reg = definitions, registry
        else:
            import os
            from pymongo import MongoClient, ASCENDING
            client = MongoClient(uri or os.environ.get("SURVEY_MONGODB_URI", "mongodb://localhost:27017"))
            database = client[db or os.environ.get("SURVEY_MONGODB_DB", "survey_agent")]
            self._defs, self._reg = database["survey_definitions"], database["survey_registry"]
            self._defs.create_index([("survey_id", ASCENDING), ("version", ASCENDING)], unique=True)

    # ---- definitions (immutable, versioned) ----
    def _latest(self, survey_id):
        latest = self._defs.find_one({"survey_id": survey_id}, sort=[("version", -1)])
        return latest["version"] if latest else None

    def put_definition(self, spec):
        sid = check_survey_id(((spec or {}).get("survey") or {}).get("id"))
        h = _hash(spec)
        latest = self._defs.find_one({"survey_id": sid}, sort=[("version", -1)])
        if latest and latest.get("content_hash") == h:
            return latest["version"]                      # unchanged -> idempotent no-op
        version = (latest["version"] + 1) if latest else 1
        stored = json.loads(json.dumps(spec))             # pin the assigned version into the stored copy
        stored.setdefault("survey", {})["version"] = version
        self._defs.insert_one({"survey_id": sid, "version": version, "content_hash": h,
                               "spec": stored, "published_at": _registry.now()})
        return version

    def get_definition(self, survey_id, version=None):
        if not valid_survey_id(survey_id):
            return None
        if version is None:
            version = (self.get_registry(survey_id) or {}).get("live_version")
            if version is None:                            # never marked live -> newest version
                version = self._latest(survey_id)
        if version is None:
            return None
        doc = self._defs.find_one({"survey_id": survey_id, "version": version})
        return doc["spec"] if doc else None

    def iter_versions(self, survey_id):
        if not valid_survey_id(survey_id):
            return []
        return [(d["version"], d.get("published_at"), d.get("content_hash"))
                for d in self._defs.find({"survey_id": survey_id}).sort("version", 1)]

    def delete_definition(self, survey_id):
        check_survey_id(survey_id)
        self._defs.delete_many({"survey_id": survey_id})
        self._reg.delete_one({"_id": survey_id})
        return True

    # ---- registry / lifecycle (mutable) ----
    def get_registry(self, survey_id):
        if not valid_survey_id(survey_id):
            return None
        reg = self._reg.find_one({"_id": survey_id})
        if reg is None:
            # A stored definition with no lifecycle (e.g. re-PUT after a delete) is a draft, NOT an
            # "unmanaged" survey the runtime would serve — it has never been approved.
            latest = self._latest(survey_id)
            return None if latest is None else {"status": "draft", "latest_version": latest}
        entry = {k: v for k, v in reg.items() if k != "_id"}
        entry.setdefault("status", _registry.derived_state(entry))
        latest = self._latest(survey_id)
        if entry.get("live_version") is None and _registry.derived_state(entry) == "live" and latest:
            # Live but unpinned (written before the pointer was always set): it was serving the newest
            # version, so pin that — from now on only an approved mark-live moves it.
            self._reg.update_one({"_id": survey_id, "live_version": None}, {"$set": {"live_version": latest}})
            entry["live_version"] = latest
        entry["latest_version"] = latest
        return entry

    def set_registry(self, survey_id, **fields):
        """Callers gate mark-live first; a mark-live write naming no `live_version` pins the newest."""
        check_survey_id(survey_id)
        if marks_live(fields) and fields.get("live_version") is None:
            latest = self._latest(survey_id)
            if latest is not None:
                fields["live_version"] = latest
        fields.pop("latest_version", None)                # derived, never stored
        fields["updated_at"] = _registry.now()
        self._reg.update_one({"_id": survey_id}, {"$set": fields}, upsert=True)
        return self.get_registry(survey_id)

    def list_surveys(self):
        out = []
        for reg in self._reg.find():
            sid = reg["_id"]
            entry = {k: v for k, v in reg.items() if k != "_id"}
            spec = (self.get_definition(sid) or {}).get("survey", {})
            out.append({
                "id": sid, "title": spec.get("title"), "description": spec.get("description"),
                "tags": spec.get("tags", []), "owner": spec.get("owner"),
                "version": reg.get("live_version") or spec.get("version"),
                "state": entry.get("status") or _registry.derived_state(entry),
                "project": entry.get("project"),
                "validated": entry.get("validated"), "tested": entry.get("tested"),
                "live": entry.get("live"), "note": entry.get("note"),
                "updated_at": entry.get("updated_at"),
            })
        return sorted(out, key=lambda e: e["id"])
