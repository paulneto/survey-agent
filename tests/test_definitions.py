"""The DefinitionStore seam: the `file` backend (today's behavior behind the interface) and the
`mongo` reference backend (auto-bump versioning, live-version resolution) with fake collections."""
import json
import os

import survey_agent.definitions as definitions
from survey_agent.definitions.file import FileDefinitionStore
from survey_agent.definitions.mongo import MongoDefinitionStore

SPEC = {"survey": {"id": "s1", "version": 1, "title": "S1", "tags": ["t"], "owner": "me",
                   "settings": {"url_variables": []},
                   "pages": [{"id": "p1", "questions": [{"id": "q1", "type": "text"}]}],
                   "actions": [{"type": "complete", "redirect": {"kind": "default"}}]}}


# ---- factory ----

def test_factory_default_is_file(monkeypatch):
    monkeypatch.delenv("SURVEY_DEFINITION_SOURCE", raising=False)
    assert isinstance(definitions.get_definition_store(), FileDefinitionStore)


def test_factory_unknown_raises():
    import pytest
    with pytest.raises(ValueError):
        definitions.get_definition_store("nope")


# ---- file backend: wraps SURVEY_DIR + .registry.json, unchanged behavior ----

def test_file_delete(tmp_path):
    d = FileDefinitionStore(dir=str(tmp_path))
    d.put_definition(SPEC)
    d.set_registry("s1", status="live")
    d.delete_definition("s1")
    assert d.get_definition("s1") is None and d.get_registry("s1") is None
    import os
    assert not os.path.exists(tmp_path / "s1.survey.json")


def test_mongo_delete_removes_versions_and_registry():
    m = _mongo()
    m.put_definition(SPEC)
    changed = json.loads(json.dumps(SPEC)); changed["survey"]["title"] = "v2"
    m.put_definition(changed)
    m.set_registry("s1", status="live", live_version=2)
    m.delete_definition("s1")
    assert m.get_definition("s1") is None and m.get_registry("s1") is None and m.iter_versions("s1") == []


def test_file_get_put_and_registry(tmp_path):
    d = FileDefinitionStore(dir=str(tmp_path))
    assert d.get_definition("s1") is None
    v = d.put_definition(SPEC)
    assert v == 1
    assert d.get_definition("s1")["survey"]["title"] == "S1"
    assert os.path.exists(tmp_path / "s1.survey.json")
    # registry round-trips through the sidecar
    d.set_registry("s1", status="live")
    reg = d.get_registry("s1")
    assert reg["status"] == "live" and reg["live_version"] == 1
    assert [e["id"] for e in d.list_surveys()] == ["s1"]
    assert d.iter_versions("s1") == [(1, None, None)]


# ---- mongo backend (fake collections) ----

class FakeCol:
    def __init__(self):
        self.docs = []
    def _m(self, d, q):
        return all(d.get(k) == v for k, v in q.items())
    def find_one(self, q, sort=None):
        ds = [d for d in self.docs if self._m(d, q)]
        for f, dr in reversed(sort or []):
            ds.sort(key=lambda d: d.get(f), reverse=dr < 0)
        return dict(ds[0]) if ds else None
    def insert_one(self, doc):
        self.docs.append(dict(doc))
    def update_one(self, q, update, upsert=False):
        for d in self.docs:
            if self._m(d, q):
                d.update(update.get("$set", {})); return
        if upsert:
            nd = dict(q); nd.update(update.get("$set", {})); self.docs.append(nd)
    def find(self, q=None):
        return _Cur([dict(d) for d in self.docs if self._m(d, q or {})])
    def delete_many(self, q):
        self.docs = [d for d in self.docs if not self._m(d, q)]
    def delete_one(self, q):
        for i, d in enumerate(self.docs):
            if self._m(d, q):
                del self.docs[i]; return

class _Cur:
    def __init__(self, ds): self.ds = ds
    def sort(self, f, dr=1): self.ds.sort(key=lambda d: d.get(f), reverse=dr < 0); return self
    def __iter__(self): return iter(self.ds)


def _mongo():
    return MongoDefinitionStore(definitions=FakeCol(), registry=FakeCol())


def test_mongo_put_autobumps_on_content_change_and_is_idempotent():
    m = _mongo()
    assert m.put_definition(SPEC) == 1
    assert m.put_definition(SPEC) == 1                 # identical content -> no new version
    changed = json.loads(json.dumps(SPEC)); changed["survey"]["title"] = "S1 v2"
    assert m.put_definition(changed) == 2              # content changed -> auto-bump
    assert [v for v, _, _ in m.iter_versions("s1")] == [1, 2]


def test_mongo_serves_live_version_then_newest():
    m = _mongo()
    m.put_definition(SPEC)
    changed = json.loads(json.dumps(SPEC)); changed["survey"]["title"] = "S1 v2"
    m.put_definition(changed)
    # no registry live pointer yet -> newest version
    assert m.get_definition("s1")["survey"]["title"] == "S1 v2"
    # pin live to v1 -> serves v1; an explicit version still fetchable
    m.set_registry("s1", live_version=1, status="live")
    assert m.get_definition("s1")["survey"]["title"] == "S1"
    assert m.get_definition("s1", version=2)["survey"]["title"] == "S1 v2"
    assert m.get_definition("nope") is None


def test_mongo_registry_and_catalog():
    m = _mongo()
    m.put_definition(SPEC)
    m.set_registry("s1", status="live", live_version=1)
    reg = m.get_registry("s1")
    assert reg["status"] == "live" and reg["live_version"] == 1 and reg["updated_at"]
    cat = m.list_surveys()
    assert cat == [{"id": "s1", "title": "S1", "description": None, "tags": ["t"], "owner": "me",
                    "version": 1, "state": "live", "project": None, "validated": None, "tested": None,
                    "live": None, "note": None, "updated_at": reg["updated_at"]}]


def test_mongo_stored_spec_version_matches_assigned():
    m = _mongo()
    bumped = json.loads(json.dumps(SPEC)); bumped["survey"]["version"] = 99  # stale field
    v = m.put_definition(bumped)
    assert v == 1 and m.get_definition("s1", version=1)["survey"]["version"] == 1
