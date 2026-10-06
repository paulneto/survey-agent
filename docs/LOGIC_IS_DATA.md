# Logic is data, not code

The central design decision in survey-agent: every rule in a survey is **data** that the engine reads,
never code that the engine runs.

> **In one sentence:** a survey is a *document* and the engine is a fixed *interpreter* of that
> document. Conditional logic, piping, repeating, quotas and routing are all bounded JSON, and there
> is no `eval()`, no scripting hook and no per-survey code anywhere in the engine.

Authoring a survey means writing a JSON file. It never means writing Python, and it never means
deploying new code. Ten thousand surveys are ten thousand JSON files run by the same engine: about
2,700 lines of pure Python whose only dependencies are the standard library plus `jsonschema`,
`PyYAML` and `regex`.

Think of **SQL**: one engine, infinite queries. You write a query (data); you don't ship a program.

---

## Why it's built this way

When survey logic can include code, it tends to end up in two places at once. The simple rules
("show Q5 if Q4 = yes") live as configuration, and everything the configuration can't express goes
into an escape hatch: a script on a question, a scripting action, a call out to someone's server.
The escape hatch is where the hard rules live, and it's the part that can't be checked, tested or
reasoned about from outside.

Three things rule that model out for survey-agent:

1. **The author is often an LLM.** If any part of a survey is code, an LLM writing a survey is an
   LLM writing code that then runs on your server, in front of real respondents. With logic as data,
   the engine only ever interprets a closed language, so the worst a bad generation can produce is a
   *wrong survey*, and the gates below are built to catch that.
2. **Surveys are fielded to real people and cost real money.** A broken branch means respondents
   screened out for no reason, quotas filled with the wrong people, and incentives paid for unusable
   data. Every reference in a survey should be resolved and every screen-out exercised *before* it
   fields, and that is only practical for a closed language.
3. **It's meant to be self-hosted by people the authors will never meet.** An operator running
   survey-agent should be able to read one small engine, audit it once, and trust every survey that
   runs on it. They can't do that if each survey brings its own code.

So the engine stays deliberately small and boring, and all the variety lives in the documents.

**The trust model, precisely.** A survey spec is operator input: someone you trust approves it. What
logic-as-data adds is that a spec an LLM drafted is *safe to run* once it passes the gates: the
engine interprets a closed language with no `eval`, `validate` checks every reference, and
`simulate` checks that completion and every declared screen-out are reachable. Being safe to run is
not the same as being ready for real people, so launching still takes a human approval (see
[AGENT_HOSTS.md](AGENT_HOSTS.md#6-what-no-agent-does-alone-field-the-survey)).

## What's new about it

Storing skip logic as configuration isn't new in itself. What survey-agent adds:

- **There is no escape hatch.** The language is closed: 6 reference kinds, 16 operators, 6 actions.
  Nothing outside the schema can be expressed, so the validator and simulator see *all* of a
  survey's logic, not just its easy parts. Custom scripting is excluded on purpose (see
  [FEATURE_COVERAGE.md](FEATURE_COVERAGE.md)).
- **One evaluator for every logic surface.** Page display, question and option visibility, grid
  rows, skips, screen-outs, quotas, webhook triggers and per-item repeat logic are the same `when`
  tree read by the same 15-line function. A rule means exactly one thing everywhere.
- **It's built for a machine author.** The schema, the validator and the simulator form a closed
  loop an LLM can run by itself: draft JSON → `validate` names every broken reference in plain
  language → `simulate` runs 500 synthetic respondents through the real engine and reports dead
  branches and the dominant screen-out → fix → repeat. The model corrects itself against what the
  engine actually does instead of guessing. The loop works because the tools can read all of the
  logic.
- **The same engine runs everywhere.** Because the engine is pure (no network, database or web
  framework), the identical code serves the live web runtime, the simulator, the conversational
  mode, the MCP inline-survey tools and the exports. What you simulated runs on the same code that fields.
- **Capabilities grow in the engine, once.** A new feature is a new piece of vocabulary, reviewed
  and tested in one place, and every survey can use it from then on. No survey ever ships code to
  get a feature.

### Worked example: adding repeating

Question and page repeating ("ask the same follow-up for each brand they picked") was added as
vocabulary, not as code in any survey:

- a `repeat` block in the schema, interpreted by one new module (`engine/repeat.py`, ~130 lines);
- one new reference kind, `loop`, and a few lines in the evaluator;
- each item's answer stored under its own key, `q_rate[netflix]`, so the **existing** evaluator,
  merge tokens, quotas and export handle repeated answers unchanged;
- new validator rules, so a bad repeat (a source on a later page, an unknown item, a loop token
  outside a loop) is rejected with a message an LLM can act on.

Every existing survey kept working untouched, and every future survey can repeat.
`surveys/streaming-brand-ratings.survey.json` shows it end to end.

## What you get

| Who | Benefit |
|---|---|
| **Researchers and operators** | A survey can be validated and simulated before it costs anything. Dead branches, unreachable screen-outs and impossible questions show up in `validate`/`simulate`, not in the field. |
| **The agent authoring the survey** | Precise, machine-readable errors and a behavioural simulation to check its work against, so it can draft, test and fix a survey without a human debugging it. |
| **Engineers and deployers** | One small engine to read, test and audit. The test suite tests the *interpreter*; survey #10,000 adds no new code paths and needs no new tests. Upgrading the engine upgrades every survey. |
| **Security reviewers** | The engine never executes anything from a survey definition: logic is a closed tree with no `eval`. LLM output is gated by a JSON Schema and a validator before it runs. A reviewer reads the engine once instead of per survey. |
| **Teams** | Surveys are documents: diffable in a PR, versioned, and analyzable in bulk. A published version is immutable and in-flight respondents stay on the version they started (the `file` source keeps version snapshots in `surveys/.versions/`). With a runtime definition source (`mongo`, `http`) there's no per-survey deploy. |

The cost is that you can't express *everything* inline. §8 covers what happens then.

---

## 1. The shape

Every condition in every survey — page display, question show/hide, option-level logic, grid-row
logic, skip, disqualify, over-quota, `set_variable` gates, webhook firing, per-item logic inside a
repeat — is the same recursive structure with exactly two node forms:

```jsonc
// leaf: compare one reference against one value
{ "ref": {"kind": "question", "id": "q_service_use"}, "operator": "equals", "value": "current" }

// group: join child nodes
{ "op": "and", "conditions": [ <node>, <node>, ... ] }
```

That is the entire logic language. A real example from `surveys/streaming-usage.survey.json` —
over-quota only for current streamers:

```json
{ "op": "and", "conditions": [
    { "ref": {"kind": "question", "id": "q_service_use"}, "operator": "equals",  "value": "current" },
    { "ref": {"kind": "quota",    "id": "quota_current"}, "operator": "is_full" }
]}
```

Groups nest arbitrarily deep, so `and`/`or` compose into any boolean expression you need.

---

## 2. The closed vocabularies

Nothing outside these lists is expressible. The JSON Schema
(`src/survey_agent/engine/schema/survey.schema.json`) rejects anything else before the engine
ever sees it.

### `ref.kind` — what a condition can read (6)

| Kind | Reads | Notes |
|---|---|---|
| `question` | a prior answer, by question id | add `"row": "<row_id>"` to read one grid cell; a repeated question's answer is `qid[item]`, or `qid[@]` for the current item |
| `url_var` | a captured inbound URL variable (`uid`, `rid`, `group`, `lang`, …) | must be declared in `settings.url_variables` |
| `variable` | a value set by a `set_variable` action or a hidden value | |
| `quota` | live quota state `{count, limit}` | pair with `is_full` / `is_not_full` |
| `enrichment` | a behavioral fact resolved from the configured `SURVEY_ENRICH` adapter | must be declared in `settings.enrichments` |
| `loop` | the current repeat item: `id` = `value`, `label`, `index` or `total` | only inside a `repeat` (see §4a) |

### `operator` — how it compares (16)

| Operator | Applies to | Behaviour |
|---|---|---|
| `equals` | scalar or list | **set-based** for lists: `[a,b] == [b,a]`. Correct for checkboxes. |
| `equals_ordered` | list | order- **and** duplicate-sensitive. Use on `ranking`, where `[a,b,c] != [c,b,a]`. |
| `not_equals` | scalar or list | negation of `equals` |
| `contains` | list or string | list membership, or substring on a string |
| `is_one_of` | scalar or list | true if any actual value intersects the expected list |
| `is_not_one_of` | scalar or list | negation of `is_one_of` |
| `is_answered` | anything | true when the value is **not** blank |
| `is_not_answered` | anything | true when the value **is** blank |
| `greater_than` | numeric | numeric coercion; a non-numeric value yields `false` |
| `less_than` | numeric | ” |
| `greater_or_equal` | numeric | ” |
| `less_or_equal` | numeric | ” |
| `between` | numeric | `"value": [lo, hi]`, inclusive both ends |
| `regex_match` | string | `re.search` semantics. `validate` rejects patterns over 200 characters, with nested unbounded quantifiers (`(x+)+`), or with an alternation inside a repeated group (`(a\|aa)+`). At most 2,000 characters of input are matched, each match has a 50 ms timeout, and a refused, invalid or timed-out pattern yields `false` |
| `is_full` | `quota` ref | `limit` is set **and** `count >= limit` |
| `is_not_full` | `quota` ref | no `limit`, **or** `count < limit` |

On a `quota` ref, any operator other than `is_full`/`is_not_full` compares against the quota's
`count`, so `{"kind":"quota", ..., "operator":"greater_than", "value":100}` is legal and means
"more than 100 in this cell so far".

`is_full` reads a snapshot, so two respondents can both pass it while racing for the last slot. The
runtime settles that at completion: a `complete` atomically reserves a slot in every quota bucket the
session falls into, all or nothing, and if any bucket is full the completion becomes the survey's
`overquota` ending (its message or redirect) instead. A session's ending is processed once: the
reservation is keyed by the response id, so a retried completion gets the same outcome. Any
`webhook` action earlier in the survey may already have fired by then.

### `action.type` — what logic can *do* (6)

| Type | Effect |
|---|---|
| `disqualify` | terminal screen-out; carries `reason_code` + optional `dq_message` / `redirect` |
| `overquota` | terminal over-quota exit |
| `complete` | terminal completion (a survey with no `complete` action fails validation) |
| `skip` | jump forward to `to_page` |
| `set_variable` | set `name` to a literal `value` or copy `value_from` a question |
| `webhook` | fire-once-per-session outbound intent (`url`, `method`, `payload`) |

Every action takes an optional `when` — the same condition tree. No `when` means unconditional.

**There is no `script` action.** That exclusion is the design, not a gap waiting to be filled.

---

## 3. The interpreter

`src/survey_agent/engine/ast_eval.py` is ~200 lines including all 16 operators. The tree-walker
itself is the last ~15:

```python
def evaluate(node, ctx):
    """Evaluate a condition node (group / condition / empty) -> bool."""
    if not node:
        return True
    if "conditions" in node and "op" in node:
        results = (evaluate(c, ctx) for c in node["conditions"])
        op = node["op"]
        if op == "and":
            return all(results)
        if op == "or":
            return any(results)
        raise ValueError(f"unknown group op: {op!r}")
    if "ref" in node and "operator" in node:
        return eval_condition(node, ctx)
    raise ValueError(f"malformed condition node: {node!r}")
```

**That one function is every logic surface in the product.** Page display, visibility, skips,
screen-outs, quotas and webhook triggers are the same `when` clause read by the same evaluator, so a
rule means exactly one thing everywhere, and the awkward edge cases get decided once, centrally.

---

## 4. Evaluation semantics you must know when authoring

These are deliberate, centralized decisions. Get them wrong and your logic silently misfires.

- **Blank-answer convention.** A comparison against an unanswered or blank value is **`False`** —
  logic only fires when there is data. The two exceptions are `is_answered` (returns `False`) and
  `is_not_answered` (returns `True`), which are evaluated *before* the blank check.
- **Fail closed on malformed input.** An invalid, refused or timed-out regex, or a `between` whose
  bound isn't a 2-element list, returns `False` rather than raising. Bad data never takes a respondent
  down a path.
- **`equals` is set-based.** Right for `checkbox`; wrong for `ranking`. Use `equals_ordered` there.
- **Omit the key; don't write `{}`.** The evaluator treats an empty node as "always true", but the
  **schema rejects a literal `"display_logic": {}`**. To mean "always show", leave the key out
  entirely. (The `{}` tolerance in `evaluate()` is defensive, for internal callers — not an
  authoring form.)
- **A condition can only read the past.** See §5.

---

## 4a. Repeating

`repeat` on a page runs the whole page once per item; on a question, it shows the question once per
item on its page. Items come from an earlier choice question (`mode: selected` or `not_selected`) or
from a number (`by: "value"` gives items `1`…`N`). Exclusive options are never repeated over.

```json
{ "id": "p_rate", "title": "{{loop:label}} ({{loop:index}} of {{loop:total}})",
  "repeat": { "source": "q_services" },
  "questions": [ { "id": "q_sat", "type": "rating", "title": "How satisfied are you with {{loop:label}}?",
                   "scale_points": [1, 2, 3, 4, 5] } ] }
```

- **Each item's answer is its own key:** `q_sat[netflix]`, `q_sat[hulu]`. Logic, merge tokens,
  quotas and export read it like any other answer. A bare `q_sat` ref is rejected, because there is
  no single answer to compare.
- **Inside a repeat**, `{{loop:label}}` / `{{loop:value}}` / `{{loop:index}}` / `{{loop:total}}`
  pipe the current item, `{"kind": "loop", "id": "value"}` tests it, and `q_sat[@]` means "this
  item's answer to `q_sat`". That last form is how a second repeated page follows up only on the
  items rated highly:

  ```json
  { "id": "p_love", "repeat": { "source": "q_services" }, "questions": [
      { "id": "q_love", "type": "textarea", "title": "What do you like most about {{loop:label}}?",
        "display_logic": { "ref": {"kind": "question", "id": "q_sat[@]"},
                           "operator": "greater_or_equal", "value": 4 } } ] }
  ```

- Passes where every question is hidden are skipped, and `index`/`total` count only the passes
  (or, for a question repeat, the instances) shown. A terminal (screen-out, over-quota, or a
  conditional `complete`) can end the survey after any pass; a `skip` waits until the loop finishes.

---

## 5. What the gates catch

Two gates, and they are complementary. This is why the workflow is `validate` **then** `simulate`,
never one or the other.

### `survey-agent validate` — structural + referential

Because logic is data, the validator can *statically* resolve every reference. It errors on:

- `page 'p_brands' display_logic: question ref references unknown question 'q_does_not_exist'`
- `question 'q_why' display_logic: depends on 'q_rate', which is on this page — the page is built
  before that answer exists, so this never shows. Move the dependent question to a later page`
- `url_var 'group' not declared in settings.url_variables`
- `enrichment 'x' not declared in settings.enrichments`
- `unknown quota ref '…'`
- `action[3] skip -> unknown page '…'`
- `action[5] set_variable -> value_from references unknown question '…'`
- `action[2] webhook -> missing 'url'`
- `duplicate question id 'q_age' — answers would overwrite each other`
- `no 'complete' action — survey has no terminal completion/redirect`
- merge tokens in titles, bodies, option labels, page titles and actions that reference an unknown
  question, undeclared url var or unset variable, or use an unknown kind (`{{qq:…}}`), field
  (`.answr`) or grid row
- piping (a merge token or `options_source`) from a question on the same or a later page
- as `[warn]`: a piped answer that can be missing (optional, hidden, on a conditional page, a URL
  variable not in the link) with no `|fallback`, and a required option-piped question that can be
  left with no options
- repeats: a source on the same or a later page, a non-choice source, option values that can't key
  per-item answers, a bare ref to a repeated question, an item the repeat never produces, `[@]` or
  `{{loop:…}}` used outside a repeat

None of that is possible once a survey can contain arbitrary code. It is the direct payoff of logic being data.

**Hence the rule:** *a question that depends on another answer goes on a later page.* The engine is
server-driven: each page is built from the answers already submitted, so a condition's inputs are
guaranteed to exist. `validate` enforces the rule.

### `survey-agent simulate` — behavioural

`validate` checks that every reference resolves. It can't tell you whether the logic *does what you meant*. A
screen-out can be perfectly well-formed and still never fire (it compares against the wrong option
value), or fire for nearly everyone, and a merge token can be valid yet render blank for the
respondents who skipped what it reads. `simulate` walks synthetic respondents through the real
engine, leaving some optional questions blank as real people do, and reports it:

```
❌ coverage gate failed:
  • screen-out 'non_user' is defined but never triggered across 100 respondents: its `when`
    condition is unreachable or wrong (a dead branch) — check the option value it compares
    against, the page order, and whether an earlier action always catches first
```

```
❌ coverage gate failed:
  • no respondent reached 'completed' — every path is caught by a screen-out first.
    Dominant screen-out(s): 'no_consent' (47/100), 'non_user' (39/100), 'under_age' (14/100).
    Loosen or reorder the disqualify whose `when` is too broad (or move it to a later page)
    so some respondents can finish.
```

Never launch on `validate` alone.

---

## 6. Why, in engineering terms

The reasoning at the top of this document, restated as properties you can check in the code:

- **No execution path from a definition.** `grep` the engine: no `eval`, no `exec`, and no
  dependencies beyond the standard library, `jsonschema`, `PyYAML` and `regex`. A survey can only
  select among behaviours the engine already has.
- **Statically checkable.** §5: every reference is checked before the survey fields.
- **Simulatable.** `simulate --n 500` uses the same `SurveyRuntime` the web server uses, so coverage
  results are about the real engine, not a model of it.
- **Tested once.** The suite tests the interpreter, so a new survey adds no untested code.
- **Pure core.** `engine/` has no network, database or web-framework code. Storage, enrichment, integrations and HTTP live outside it
  (`store/`, `enrich/`, `integrations/`, `server.py`, `api/`), which is what lets one engine serve
  every surface.

---

## 7. The one place we generate code

Enrichment, and only in an adapter that queries a database. An enrichment *spec* (also data:
`event_type` + `window` + `criteria` + `operation`) is compiled into a query against your warehouse.
In the reference `firebolt` adapter (`enrich/firebolt_sql.py`), SQL identifiers are validated
against a strict pattern and every value is passed as a bound parameter.

That is deterministic compilation of bounded data into a query, still not arbitrary code and still
no per-survey software. It lives outside the engine, and it is the seam to review when auditing
this claim (along with any adapter you write yourself).

---

## 8. The cost, stated plainly

**You cannot express everything.** If a survey needs logic the 16 operators can't say, the answer is
*not* "write a script in the survey". It is:

1. add the operator (or reference kind, or block like `repeat`) to the schema,
2. implement it once in the engine,
3. teach the validator to check it,
4. add tests,
5. every survey can now use it.

That is a real code change, reviewed once, in the one place that carries the tests. Slower for
the exotic case, and deliberately so; it's the same bargain SQL makes. Repeating went through
exactly these steps (see "Worked example" above).

---

## See also

- [ENGINEERING.md](ENGINEERING.md) — full architecture and the three layers (definition / engine / runtime)
- [AGENTS.md](../AGENTS.md) — how an LLM drives the author → validate → simulate → approve → launch loop
- [FEATURE_COVERAGE.md](FEATURE_COVERAGE.md) — the capability matrix, including what is out of scope
- `src/survey_agent/engine/ast_eval.py` — the evaluator
- `src/survey_agent/engine/repeat.py` — repeating
- `surveys/streaming-brand-ratings.survey.json` — a worked example of repeating and per-item logic
- `src/survey_agent/engine/schema/survey.schema.json` — the authoritative grammar
