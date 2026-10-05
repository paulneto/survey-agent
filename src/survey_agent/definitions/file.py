"""file DefinitionStore — today's behavior behind the seam: definitions are `.survey.json` files in
`SURVEY_DIR`, lifecycle is the mutable `.registry.json` sidecar (`registry.py`). One version per file
(git history is the trail). This is the default; routing the runtime through it changes nothing.
"""
import json
import os

from survey_agent import registry
from .base import DefinitionStore


class FileDefinitionStore(DefinitionStore):
    def __init__(self, dir=None):
        self.dir = dir or registry.surveys_dir()

    def _file_for(self, survey_id):
        """Resolve id → path the same way the runtime did: catalog match, else <id>.survey.json."""
        for e in registry.catalog(self.dir):
            if e.get("id") == survey_id and e.get("file"):
                return os.path.join(self.dir, e["file"])
        cand = os.path.join(self.dir, f"{survey_id}.survey.json")
        return cand if os.path.isfile(cand) else None

    def get_definition(self, survey_id, version=None):
        path = self._file_for(survey_id)
        if not path:
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def put_definition(self, spec):
        s = spec.get("survey", {})
        sid = s["id"]
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, f"{sid}.survey.json"), "w") as f:
            json.dump(spec, f, indent=2)
            f.write("\n")
        return s.get("version", 1)

    def get_registry(self, survey_id):
        entry = registry.load(self.dir)["surveys"].get(survey_id)
        if entry is None:
            spec = self.get_definition(survey_id)
            if spec is None:
                return None
            entry = {}
        live_v = (self.get_definition(survey_id) or {}).get("survey", {}).get("version", 1)
        return {**entry, "status": registry.derived_state(entry), "live_version": live_v}

    def set_registry(self, survey_id, **fields):
        return registry.record(survey_id, sdir=self.dir, **fields)

    def list_surveys(self):
        return registry.catalog(self.dir)

    def iter_versions(self, survey_id):
        spec = self.get_definition(survey_id)
        if not spec:
            return []
        return [(spec.get("survey", {}).get("version", 1), None, None)]

    def delete_definition(self, survey_id):
        path = self._file_for(survey_id)
        if path and os.path.exists(path):
            os.remove(path)
        data = registry.load(self.dir)
        if survey_id in data.get("surveys", {}):
            del data["surveys"][survey_id]
            registry.save(data, self.dir)
        return True
