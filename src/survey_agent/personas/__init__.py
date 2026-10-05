"""Persona sources — where the synthetic respondents for `pretest` come from. A **seam**, like
`store` / `integrations` / `enrich`: the open core ships `synthetic` (an LLM, or the offline stub,
generates personas from the survey or a declared *audience*); a deployer plugs in **real-data-grounded**
personas — profiles sampled from a panel / behavioral cohort — by pointing `SURVEY_PERSONAS` at an
import path (`my_pkg.personas:RealPersonaSource`).

== Persona format (what a source's `generate()` returns) ==

    {
      "label":   "current heavy streamer",      # short human label (display + LLM role-play)
      "traits":  {"age": 34, "region": "US",     # freeform profile (str/number values)
                  "usage": "current", "price_sensitivity": "high"},
      "answers": {"q_service_use": "current",    # OPTIONAL exact answers, keyed by QUESTION ID
                  "q_age": 34},                  #   (validated by the engine; wins over traits)
      "segment": "current heavy"                 # OPTIONAL grouping label for the report
    }

- **`traits`** — freeform key→value describing the person. Drives the LLM role-play, and offline is
  matched fuzzily against your option labels/values for coherence (so use values that resemble the
  option text, e.g. `usage: "never"`). This is the easy, approximate path.
- **`answers`** — OPTIONAL, the **exact** path: question id → answer value, validated by the engine
  (no fuzzy matching). **Best practice for a real-data plugin that knows the survey:** emit `answers`
  for the questions you have real data for, and `traits` so the LLM role-plays the rest.
- A bare string is accepted and becomes `{"label": "<str>", "traits": {}}`.

== Audience format (the population to test against) ==

    {"n": 200, "segments": [ {<persona>, "weight": 0.5}, {<persona>, "weight": 0.5} ]}

`n` is allocated across segments proportionally to `weight` (deterministic). Shorthands: a single
`{"traits": {...}}`, a bare list of personas, or `null` (the source invents personas from the survey).
Validate a spec with `validate_audience(spec)`; the JSON Schema is `audience.schema.json`.

**TESTING only** — QA / pre-flight (logic, UX, incidence calibration), NOT a substitute for fielding
to real people. Answers are role-played + VALIDATED by the engine; a real-data source makes the test
realistic, but it's still a test.
"""
import json
import os

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "audience.schema.json")


class PersonaSource:
    """Implement `generate(survey, audience, n, seed) -> list[persona]`. The default `synthetic`
    source is below; a real-data source pulls profiles matching the audience from a panel/warehouse
    and returns the same persona shape (label / traits / optional answers / optional segment)."""
    name = "base"

    def generate(self, survey, audience=None, n=6, seed=7):
        raise NotImplementedError


def load_audience(spec_or_path):
    """Normalize an audience: a path to a JSON file, a dict, a list of personas, or None."""
    if isinstance(spec_or_path, str):
        with open(spec_or_path) as f:
            return json.load(f)
    return spec_or_path


def validate_audience(spec):
    """Best-effort validation of an audience / persona spec against `audience.schema.json`. Returns a
    list of error strings ([] = valid). Handy for a persona_source plugin to check its own output."""
    spec = load_audience(spec)
    try:
        import jsonschema
    except ImportError:
        return ["[warn] jsonschema not installed — skipped audience schema validation"]
    with open(SCHEMA_PATH) as f:
        schema = json.load(f)
    return [f"/{'/'.join(map(str, e.path))}: {e.message}"
            for e in sorted(jsonschema.Draft202012Validator(schema).iter_errors(spec),
                            key=lambda e: list(e.path))]


def _raw_personas(audience):
    """[(weight, persona_dict)] from an audience — preserves `label`/`answers` (unlike `_segments`,
    which is the traits-only view kept for matching inside custom sources)."""
    if not audience:
        return []
    if isinstance(audience, dict) and audience.get("segments"):
        return [(float(s.get("weight", 1)), s) for s in audience["segments"]]
    if isinstance(audience, dict) and (audience.get("traits") or audience.get("answers")):
        return [(1.0, audience)]
    if isinstance(audience, list):
        return [(float(p.get("weight", 1)) if isinstance(p, dict) else 1.0,
                 p if isinstance(p, dict) else {"label": str(p)}) for p in audience]
    return []


def _segments(audience):
    """[(weight, traits, label)] — the traits-only view (back-compat; handy for matching in plugins)."""
    return [(w, p.get("traits", {}) or {}, p.get("label")) for w, p in _raw_personas(audience)]


def _label_from_traits(traits, i=0):
    if not traits:
        return f"respondent {i + 1}"
    return "; ".join(f"{k}={v}" for k, v in list(traits.items())[:3])


class SyntheticPersonaSource(PersonaSource):
    """LLM (or offline stub) personas. With an `audience`, allocates n respondents across its segments
    *proportionally to weight* (deterministic) and carries each segment's traits + optional exact
    answers. Without one, asks the LLM to invent personas from the survey — the original behavior."""
    name = "synthetic"

    def generate(self, survey, audience=None, n=6, seed=7):
        raw = _raw_personas(audience)
        if raw:
            total = sum(w for w, _ in raw) or 1.0
            out, k = [], 0
            for w, base in raw:
                traits = base.get("traits", {}) or {}
                label = base.get("label") or _label_from_traits(traits, k)
                for _ in range(round(n * w / total)):     # proportional, deterministic allocation
                    p = {"label": label, "traits": dict(traits), "segment": base.get("segment") or label}
                    if base.get("answers"):
                        p["answers"] = dict(base["answers"])
                    out.append(p)
                    k += 1
            while len(out) < n and out:                    # rounding drift -> pad from the first
                out.append(dict(out[0]))
            return out[:n]
        # no audience -> let the model invent personas from the survey (trait-less)
        import survey_agent.ai as ai
        try:
            arr = ai.get_llm().personas(survey, n)
        except Exception:
            arr = ai._FALLBACK_PERSONAS[:n]
        return [{"label": s, "traits": {}, "segment": None} for s in arr]


def get_persona_source(kind=None, **kwargs):
    """Resolve a persona source: built-in `synthetic`, or a deployer plugin via an import path
    ('my_pkg.personas:RealPersonaSource'). Selected by SURVEY_PERSONAS (default: synthetic)."""
    from survey_agent import env_select
    kind = (kind or env_select("SURVEY_PERSONAS", "synthetic")) or "synthetic"
    if kind.lower() in ("synthetic", ""):
        return SyntheticPersonaSource()
    if "." in kind or ":" in kind:                          # bring-your-own (e.g. real panel data)
        import importlib
        mod, _, cls = kind.replace(":", ".").rpartition(".")
        if not mod:
            raise ValueError(f"SURVEY_PERSONAS={kind!r} is not an import path (use 'pkg.module:ClassName')")
        return getattr(importlib.import_module(mod), cls)(**kwargs)
    raise ValueError(f"unknown SURVEY_PERSONAS: {kind!r} (built-in 'synthetic', or an import path "
                     f"'my_pkg.personas:RealPersonaSource' for real-data-grounded personas)")
