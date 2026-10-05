# Persona sources & the audience format

`survey-agent pretest` runs **synthetic respondents** through a survey to preview incidence, screen-outs,
and logic *before* you field to real people. Where those respondents come from is a **seam**
(`SURVEY_PERSONAS`), like storage/integrations/enrichment:

- **`synthetic`** (default): an LLM (or the offline stub) role-plays personas generated from the
  survey or a **declared audience**.
- **a plugin** (`SURVEY_PERSONAS=my_pkg.personas:RealPersonaSource`): **real-data-grounded** personas
  sampled from a panel / behavioral cohort. Same format; the test becomes realistic.

> **Testing only.** This is QA and pre-flight: logic, UX, incidence calibration. Answers are role-played
> and **validated by the engine**, but synthetic respondents are **not** a substitute for fielding to
> real people. A real-data source makes the *test* realistic; it's still a test.

## The persona format (what a source returns)

A persona is a small object. Only `label` is conventional; everything else is optional:

```jsonc
{
  "label":   "current heavy streamer",        // short human label (display + LLM role-play)
  "traits":  { "age": 34, "region": "US",      // freeform key→value profile (string/number)
               "usage": "current", "price_sensitivity": "high" },
  "answers": { "q_service_use": "current",     // OPTIONAL exact answers, keyed by QUESTION ID
               "q_age": 34 },                  //   (validated by the engine; wins over traits)
  "segment": "current heavy"                   // OPTIONAL grouping label for the report
}
```

Two ways to drive answers, use either or **both**:

| | `traits` (fuzzy) | `answers` (exact) |
|---|---|---|
| **What** | freeform profile | `{question_id: value}` |
| **How it answers** | LLM role-plays it; offline, trait values are matched against your option labels/values | used verbatim, then validated by the engine |
| **Use when** | you want realistic, approximate behavior (open-ends, unmapped questions) | you know the survey and want deterministic, unambiguous answers |
| **Best practice** | always include some, for role-play | a real-data plugin should set these for the questions it has data for |

A bare string also works and becomes `{"label": "<str>", "traits": {}}`.

## The audience format (the population to test against)

```jsonc
{ "n": 200, "segments": [
    { "label": "current", "weight": 0.6, "traits": { "consent": "yes", "usage": "current", "age": 30 } },
    { "label": "never",   "weight": 0.4, "traits": { "consent": "yes", "usage": "never" } } ] }
```

`n` respondents are allocated across `segments` **proportionally to `weight`** (deterministic).
Shorthands: a single `{ "traits": {…} }`, a bare list of personas, or `null` (the source invents
personas from the survey). Worked example: [`audiences/streaming-audience.json`](../audiences/streaming-audience.json).

Use it: `survey-agent pretest <survey> --audience audiences/streaming-audience.json` (or a one-off
`--trait usage=never`), and `survey-agent author … --audience <file>` to test the incidence gate against it.

**Validate** an audience/persona spec against the shipped JSON Schema
([`personas/audience.schema.json`](../src/survey_agent/personas/audience.schema.json)):

```python
from survey_agent.personas import validate_audience
errs = validate_audience("audiences/streaming-audience.json")   # [] == valid
```

## Writing a `persona_source` plugin (real data)

Implement one method and return the persona shape above:

```python
# my_pkg/personas.py   →   set SURVEY_PERSONAS=my_pkg.personas:RealPersonaSource
from survey_agent.personas import PersonaSource, _segments

class RealPersonaSource(PersonaSource):
    name = "panel"
    def generate(self, survey, audience=None, n=6, seed=7):
        criteria = _segments(audience)            # [(weight, traits, label)] to match against your data
        profiles = my_panel.sample(criteria, n)    # your panel / behavioral cohort
        return [{
            "label":   p.id,
            "traits":  {"age": p.age, "region": p.region, "usage": p.streaming_segment},
            "answers": {"q_age": p.age},           # set exact answers for questions you have real data for
            "segment": p.cohort,
        } for p in profiles]
```

`generate(survey, audience, n, seed)` receives the survey spec, the (already-normalized) audience, a
target count, and a seed. Return a list of personas. The engine still owns flow/validation; you supply
**who** the respondents are (and optionally their exact answers); the LLM role-plays the rest.

See also: [AGENTIC_MODES.md](AGENTIC_MODES.md) · [PRIMITIVE.md](PRIMITIVE.md).
