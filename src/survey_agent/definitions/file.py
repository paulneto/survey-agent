"""file DefinitionStore — definitions are `.survey.json` files in `SURVEY_DIR`, lifecycle is the
mutable `.registry.json` sidecar (`registry.py`). This is the default backend.

Versioning: `<id>.survey.json` stays the one current, editable file people see and commit. Every
version that is published or served is also kept as an **immutable snapshot** in
`<SURVEY_DIR>/.versions/<id>/v<N>.survey.json`, so `get_definition(id, version=N)` returns exactly
what version N was — a respondent who started on v1 finishes on v1 after v2 ships.

The version-number contract matches the mongo backend: a snapshot is never overwritten.
  * identical content (ignoring the `version` field) to the newest snapshot → no-op, same version;
  * changed content whose `version` is new and higher than every snapshot → that version (an
    author's explicit bump is honoured);
  * changed content that reuses an existing version number (or goes backwards) → auto-bumped to
    newest + 1, and that assigned version is written back into the current file on `put`.
A hand edit of `<id>.survey.json` that forgets to bump `version` is reconciled the same way the first
time it is read (it gets the next version, as a snapshot) — the authored file itself is only
rewritten by `put_definition`, never by a read. Inspection-only callers (e.g. `survey-agent dsar`) set
`snapshot_on_read = False`: versions are still computed, nothing is written.

Commit `.versions/` alongside the surveys if you deploy by git: it is the immutability trail that lets
in-flight sessions finish on the version they started. The dot-directory is invisible to the survey
catalog / registry / `*.survey.json` globs (they list the top level of SURVEY_DIR only).
"""
import datetime
import json
import os
import re
import shutil
import threading

from survey_agent import registry
from .base import DefinitionStore
from .mongo import _hash

VERSIONS_DIR = ".versions"
_SNAP_RE = re.compile(r"^v(\d+)\.survey\.json$")


def _as_version(v, default=1):
    try:
        n = int(v)
        return n if n >= 1 else default
    except (TypeError, ValueError):
        return default


def _safe_id(survey_id):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(survey_id)).strip(".") or "_"


def _dump(path, spec):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(spec, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _pin(spec, version):
    out = json.loads(json.dumps(spec))
    out.setdefault("survey", {})["version"] = version
    return out


class FileDefinitionStore(DefinitionStore):
    def __init__(self, dir=None, snapshot_on_read=True):
        self.dir = dir or registry.surveys_dir()
        self.snapshot_on_read = snapshot_on_read
        self._lock = threading.RLock()
        self._snap_hash = {}      # snapshot path -> content hash (snapshots are immutable)
        self._resolved = {}       # current-file path -> ((mtime_ns, size), version)

    # ---- paths ----
    def _file_for(self, survey_id):
        """Resolve id → path the same way the runtime did: catalog match, else <id>.survey.json."""
        for e in registry.catalog(self.dir):
            if e.get("id") == survey_id and e.get("file"):
                return os.path.join(self.dir, e["file"])
        cand = os.path.join(self.dir, f"{survey_id}.survey.json")
        return cand if os.path.isfile(cand) else None

    def _vdir(self, survey_id):
        return os.path.join(self.dir, VERSIONS_DIR, _safe_id(survey_id))

    def _snap_path(self, survey_id, version):
        return os.path.join(self._vdir(survey_id), f"v{version}.survey.json")

    def _snapshots(self, survey_id):
        """{version: path} of the immutable snapshots for a survey."""
        d = self._vdir(survey_id)
        out = {}
        if os.path.isdir(d):
            for name in os.listdir(d):
                m = _SNAP_RE.match(name)
                if m:
                    out[int(m.group(1))] = os.path.join(d, name)
        return out

    def _load(self, path):
        try:
            with open(path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _hash_of(self, path):
        h = self._snap_hash.get(path)
        if h is None:
            spec = self._load(path)
            h = _hash(spec) if spec is not None else None
            if h is not None:
                self._snap_hash[path] = h
        return h

    # ---- version assignment (shared by put + read reconciliation) ----
    def _assign(self, survey_id, spec):
        """Return (version, is_new) for this content: an existing snapshot's version when the content
        matches it (the stated version's snapshot, else the newest), otherwise a fresh number per the
        module contract. Caller holds self._lock."""
        h = _hash(spec)
        stated = _as_version(spec.get("survey", {}).get("version"))
        snaps = self._snapshots(survey_id)
        if stated in snaps and self._hash_of(snaps[stated]) == h:
            return stated, False
        if snaps:
            newest = max(snaps)
            if self._hash_of(snaps[newest]) == h:
                return newest, False
            return (stated if stated > newest else newest + 1), True
        return stated, True

    def _snapshot(self, survey_id, version, spec):
        """Write an immutable snapshot (never overwrites an existing one)."""
        path = self._snap_path(survey_id, version)
        if os.path.exists(path):
            raise FileExistsError(f"definition snapshot {survey_id} v{version} already exists")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        pinned = _pin(spec, version)
        _dump(path, pinned)
        self._snap_hash[path] = _hash(pinned)

    def _current(self, survey_id):
        """The current file reconciled against the snapshots → (spec pinned to its version, version),
        or (None, None). Snapshots a not-yet-seen version (best-effort: a read-only SURVEY_DIR just
        serves the file as-is)."""
        path = self._file_for(survey_id)
        if not path:
            return None, None
        spec = self._load(path)
        if spec is None:
            return None, None
        try:
            st = os.stat(path)
            stamp = (st.st_mtime_ns, st.st_size)
        except OSError:
            stamp = None
        with self._lock:
            cached = self._resolved.get(path)
            if cached and stamp and cached[0] == stamp \
                    and os.path.exists(self._snap_path(survey_id, cached[1])):
                return _pin(spec, cached[1]), cached[1]
            version, new = self._assign(survey_id, spec)
            if new and not self.snapshot_on_read:
                return _pin(spec, version), version
            if new:
                try:
                    self._snapshot(survey_id, version, spec)
                except OSError:
                    return spec, _as_version(spec.get("survey", {}).get("version"))
            if stamp:
                self._resolved[path] = (stamp, version)
        return _pin(spec, version), version

    # ---- DefinitionStore ----
    def get_definition(self, survey_id, version=None):
        if version is None:
            return self._current(survey_id)[0]
        version = _as_version(version, default=None)
        if version is None:
            return None
        snap = self._snapshots(survey_id).get(version)
        if snap:
            return self._load(snap)
        spec, cur_v = self._current(survey_id)       # first read may have just snapshotted it
        if spec is not None and cur_v == version:
            return spec
        return None

    def put_definition(self, spec):
        s = spec.get("survey", {})
        sid = s["id"]
        os.makedirs(self.dir, exist_ok=True)
        with self._lock:
            version, new = self._assign(sid, spec)
            if new:
                self._snapshot(sid, version, spec)
            path = self._file_for(sid) or os.path.join(self.dir, f"{sid}.survey.json")
            _dump(path, _pin(spec, version))        # the editable current file carries the assigned version
            self._resolved.pop(path, None)
        return version

    def get_registry(self, survey_id):
        entry = registry.load(self.dir)["surveys"].get(survey_id)
        _, live_v = self._current(survey_id)
        if entry is None:
            if live_v is None:
                return None
            entry = {}
        return {**entry, "status": registry.derived_state(entry), "live_version": live_v or 1}

    def set_registry(self, survey_id, **fields):
        return registry.record(survey_id, sdir=self.dir, **fields)

    def list_surveys(self):
        return registry.catalog(self.dir)

    def iter_versions(self, survey_id):
        _, cur_v = self._current(survey_id)          # make sure the current file has its snapshot
        out = []
        for v, path in sorted(self._snapshots(survey_id).items()):
            try:
                ts = datetime.datetime.fromtimestamp(os.path.getmtime(path), datetime.timezone.utc)
                published = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
            except OSError:
                published = None
            out.append((v, published, self._hash_of(path)))
        if cur_v is not None and cur_v not in {v for v, _, _ in out}:   # read-only: not yet snapshotted
            out.append((cur_v, None, _hash(self._current(survey_id)[0])))
        return out

    def delete_definition(self, survey_id):
        with self._lock:
            path = self._file_for(survey_id)
            if path and os.path.exists(path):
                os.remove(path)
                self._resolved.pop(path, None)
            vdir = self._vdir(survey_id)
            if os.path.isdir(vdir):
                shutil.rmtree(vdir)
                self._snap_hash = {p: h for p, h in self._snap_hash.items()
                                   if not p.startswith(vdir + os.sep)}
        data = registry.load(self.dir)
        if survey_id in data.get("surveys", {}):
            del data["surveys"][survey_id]
            registry.save(data, self.dir)
        return True
