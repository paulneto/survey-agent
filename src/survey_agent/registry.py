"""Survey registry — a tiny operational index over the flat survey files.

The survey *definitions* stay the source of truth (immutable, versioned `.survey.json` files in
`SURVEY_DIR`). This adds the *operational* layer an operator needs to manage a SERIES of surveys:
which exist, what they're for, whether they've been validated / tested, and whether they're live.

Two layers, kept apart on purpose (the project's "definitions are immutable" rule):

  • Definition metadata  — `title`, `description`, `tags`, `owner` live IN each `.survey.json`,
    versioned with the survey (purpose/context travels with the file).
  • Lifecycle state      — validated / tested / live + a free-text note are MUTABLE operational
    facts, so they live in a sidecar `.registry.json` next to the surveys. Editing them never
    mutates an immutable definition.

The sidecar is updated best-effort by the CLI (`author` / `validate` / `simulate`) and the MCP
connector (`save_survey` / `simulate_survey`), and read by `survey-agent list` / `status` and the
MCP `list_surveys` tool. Pure stdlib — no engine, network, or framework imports.
"""
import json
import os
import time

REGISTRY_FILE = ".registry.json"

# explicit lifecycle states an operator can set (beyond the ones we derive from signals)
STATES = ["draft", "validated", "tested", "approved", "live", "paused", "closed"]


def surveys_dir(explicit=None):
    """Resolve the surveys directory: an explicit value wins, else $SURVEY_DIR, else `surveys/`."""
    return explicit or os.environ.get("SURVEY_DIR") or "surveys"


def now():
    """UTC timestamp (stdlib only)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _path(sdir):
    return os.path.join(sdir, REGISTRY_FILE)


def load(sdir=None):
    """Read the sidecar (always returns a dict with a `surveys` map; empty if absent/corrupt)."""
    sdir = surveys_dir(sdir)
    try:
        with open(_path(sdir)) as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("surveys", {})
    return data


def save(data, sdir=None):
    sdir = surveys_dir(sdir)
    os.makedirs(sdir, exist_ok=True)
    with open(_path(sdir), "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def record(survey_id, sdir=None, **fields):
    """Merge `fields` into one survey's lifecycle entry and stamp `updated_at`. Best-effort: never
    raises (a registry hiccup must not break the command that triggered it)."""
    if not survey_id:
        return None
    try:
        data = load(sdir)
        entry = data["surveys"].setdefault(survey_id, {})
        entry.update(fields)
        entry["updated_at"] = now()
        save(data, sdir)
        return entry
    except Exception:
        return None


def record_validation(survey_id, ok, sdir=None):
    return record(survey_id, sdir=sdir, validated={"ok": bool(ok), "at": now()})


def record_test(survey_id, cov, incidence=None, sdir=None):
    """Record a coverage-simulation result (gate pass/fail + estimated LOI + optional incidence)."""
    tested = {"at": now(), "n": cov.get("n"),
              "gate": "passed" if not cov.get("problems") else "failed",
              "loi_min": round((cov.get("loi_seconds") or 0) / 60, 1) or None}
    if incidence is not None:
        tested["incidence"] = round(incidence, 3)
    return record(survey_id, sdir=sdir, tested=tested)


def set_status(survey_id, status=None, note=None, url=None, project=None, sdir=None):
    """Operator override: set an explicit lifecycle state, attach a note / live URL, and/or move the
    survey to a project (a mutable organizational group; `""` clears it). `None` leaves a field as-is."""
    fields = {}
    if status is not None:
        fields["status"] = status
    if status == "live" or url is not None:
        live = {"at": now()} if status == "live" else {}
        if url is not None:
            live["url"] = url
        fields["live"] = live
    if note is not None:
        fields["note"] = note
    if project is not None:
        fields["project"] = project.strip() or None        # "" → ungrouped
    return record(survey_id, sdir=sdir, **fields)


def derived_state(entry):
    """Collapse the recorded signals into one lifecycle label. An explicit `status` an operator set
    wins for terminal/manual states; otherwise we infer from validated/tested/live signals."""
    st = entry.get("status")
    if st in ("approved", "live", "paused", "closed"):
        return st
    if (entry.get("live") or {}).get("at"):
        return "live"
    t = entry.get("tested") or {}
    if t.get("gate") == "passed":
        return "tested"
    if t.get("gate") == "failed":
        return "tested(failing)"
    if (entry.get("validated") or {}).get("ok"):
        return "validated"
    return st or "draft"


def catalog(sdir=None):
    """Merge survey-file metadata with sidecar state into a sorted list — the data behind
    `survey-agent list` and the MCP `list_surveys` tool. Surveys with a registry entry but no file
    (e.g. deleted) are surfaced too, flagged `missing_file`."""
    sdir = surveys_dir(sdir)
    reg = load(sdir)["surveys"]
    out, seen = [], set()
    if os.path.isdir(sdir):
        for name in sorted(os.listdir(sdir)):
            if not name.endswith(".survey.json"):
                continue
            try:
                with open(os.path.join(sdir, name)) as f:
                    s = json.load(f).get("survey", {})
            except (OSError, ValueError):
                continue
            sid = s.get("id") or name[: -len(".survey.json")]
            entry = reg.get(sid, {})
            seen.add(sid)
            out.append({
                "id": sid, "title": s.get("title"), "description": s.get("description"),
                "tags": s.get("tags", []), "owner": s.get("owner"),
                "version": s.get("version", 1), "file": name,
                "state": derived_state(entry),
                "project": entry.get("project"),               # mutable organizational group (registry, not the def)
                "validated": entry.get("validated"), "tested": entry.get("tested"),
                "live": entry.get("live"), "note": entry.get("note"),
                "updated_at": entry.get("updated_at"),
            })
    for sid, entry in sorted(reg.items()):
        if sid not in seen:
            out.append({"id": sid, "title": None, "file": None, "missing_file": True,
                        "state": derived_state(entry), "project": entry.get("project"),
                        "tested": entry.get("tested"),
                        "live": entry.get("live"), "note": entry.get("note"),
                        "updated_at": entry.get("updated_at")})
    return out
