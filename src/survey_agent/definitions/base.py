"""The DefinitionStore interface — where survey *definitions* (the versioned specs) and their
*lifecycle* (status / which version is live) are read from and published to.

This is a **backend-agnostic seam**, NOT a database feature: the engine talks to this interface and
never imports a specific store. `file` (the default — definitions in `SURVEY_DIR`, lifecycle in the
`.registry.json` sidecar) and `mongo` (a reference runtime backend) ship in the box; a deployer can
point `SURVEY_DEFINITION_SOURCE` at their own (`your_pkg:YourDefinitions`). Mongo is *a* reference
backend, not a required dependency — exactly like `SURVEY_STORE=mongo`.

With a runtime backend, `publish` and `set_registry` (pause/close) are store writes — instantly live,
no app redeploy — and version-pinning is real (in-flight sessions keep the `(id, version)` they
started on). The local `.survey.json` stays the authored artifact + readable git-backed backup.
"""


class DefinitionStore:
    def get_definition(self, survey_id, version=None):
        """The full Survey spec ({"survey": {...}}) for a survey id. `version=None` → the live version.
        Returns None if unknown."""
        raise NotImplementedError

    def put_definition(self, spec):
        """Store a definition immutably and return its version. Backends that version (mongo) auto-bump
        on a content-hash change (identical content is a no-op); the file backend writes the file."""
        raise NotImplementedError

    def get_registry(self, survey_id):
        """Lifecycle for a survey id: {status, live_version, live, note, validated, tested, ...} or None."""
        raise NotImplementedError

    def set_registry(self, survey_id, **fields):
        """Merge lifecycle fields (status / live_version / note / live url). Mutable; never a redeploy."""
        raise NotImplementedError

    def list_surveys(self):
        """Operational catalog (id, title, state, …) behind `survey-agent list` / the multi-landing."""
        raise NotImplementedError

    def iter_versions(self, survey_id):
        """[(version, published_at, content_hash), …] newest-last — for `export --all` / history."""
        raise NotImplementedError

    def delete_definition(self, survey_id):
        """Remove a survey's definition (all versions) + its lifecycle entry. Collected responses are
        NOT touched — they live in the response store and stay for export/retention."""
        raise NotImplementedError
