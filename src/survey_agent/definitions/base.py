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

**Authored vs approved.** Saving a definition (`put_definition`) only adds a version; it NEVER moves
what respondents are served. The registry's `live_version` is the *approved* pointer: it is set only
by an approval-gated registry write (`set_registry` with `status="live"` / `live.at` / an explicit
`live_version`, which every surface gates with `config.may_mark_live`). `get_registry` reports the
newest authored version separately as `latest_version`. An approved mark-live that names no
`live_version` pins the newest authored version at that moment.

Survey ids are strict (`SURVEY_ID_RE`, the same pattern as the JSON Schema) because backends turn them
into file paths, keys and URLs: reads of an invalid id return None / [], writes raise
`InvalidSurveyId` (a ValueError).
"""
import re

SURVEY_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class InvalidSurveyId(ValueError):
    """A survey id outside `SURVEY_ID_RE` (empty, path separators, `..`, absolute paths, …)."""


def valid_survey_id(survey_id):
    return isinstance(survey_id, str) and bool(SURVEY_ID_RE.fullmatch(survey_id))


def check_survey_id(survey_id):
    """Return the id unchanged, or raise InvalidSurveyId."""
    if not valid_survey_id(survey_id):
        raise InvalidSurveyId(f"invalid survey id {survey_id!r}: use letters, digits, '_' and '-' only")
    return survey_id


def marks_live(fields):
    """True when a registry write is a mark-live act (explicit `status: live` or a `live.at` stamp)."""
    return fields.get("status") == "live" or bool((fields.get("live") or {}).get("at"))


class DefinitionStore:
    def get_definition(self, survey_id, version=None):
        """The full Survey spec ({"survey": {...}}) for a survey id. `version=None` → the live version.
        Returns None if unknown."""
        raise NotImplementedError

    def put_definition(self, spec):
        """Store a definition immutably and return its version. Backends that version (mongo) auto-bump
        on a content-hash change (identical content is a no-op); the file backend writes the file.
        Never changes the registry's `live_version` — publishing content is not approving it."""
        raise NotImplementedError

    def get_registry(self, survey_id):
        """Lifecycle for a survey id: {status, live_version, latest_version, live, note, validated,
        tested, ...} or None. `live_version` is the approved pointer (what is served); `latest_version`
        the newest authored version. Never moved by `put_definition`."""
        raise NotImplementedError

    def set_registry(self, survey_id, **fields):
        """Merge lifecycle fields (status / live_version / note / live url). Mutable; never a redeploy.
        Callers gate mark-live writes (config.may_mark_live) BEFORE calling; a mark-live write without
        an explicit `live_version` pins the newest authored version."""
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
