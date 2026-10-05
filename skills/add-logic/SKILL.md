---
name: add-logic
description: Add or fix skip/branch logic, disqualify/screen-out rules, attention checks, piping, question/page repeating, behavioral enrichment, quotas, or routing-into-task terminals in a survey-agent survey. Triggers on "add skip logic", "branch on…", "screen out…", "add an attention check", "pipe…", "repeat for each…", "ask the same follow-up for every brand", "loop over…", "route qualified people to…", "ask why they searched…", "add a quota".
---

# add-logic

Insert or repair the conditional behavior of a survey: display logic, skip/branch, disqualify,
quotas, piping, repeating, behavioral enrichment, and routing terminals.

## Building blocks

- **Condition AST** (`when` / `display_logic`): `{op: and|or, conditions:[…]}` or
  `{ref:{kind, id}, operator, value}`. `ref.kind ∈ question | url_var | variable | quota | enrichment | loop`
  (`loop` only inside a repeat). Operators are the schema's enum (`equals`, `is_one_of`,
  `greater_than`, `between`, `regex_match`, `is_full`, …). Logic is data: a closed AST, never code.
- **Actions** (ordered; precedence disqualify > overquota > skip > complete):
  `disqualify` (`dq_message`, `reason_code`), `overquota` (gate on `{ref:{kind:quota},operator:is_full}`),
  `skip` (`to_page`), `set_variable` (`name` + `value`/`value_from`), `webhook` (notify an external
  system mid-survey; see below), `complete` (`redirect`: `{kind:route, task_url}` to send qualified
  respondents on to a task or next step, or `{kind:default}`).
- **Webhook action** (`{type:webhook, when, url, method?, payload?}`): when `when` is true, the engine
  emits a resolved HTTP intent **once per session**. `url` and `payload` resolve merge tokens
  (`{{q:…}}`/`{{url:…}}`/`{{var:…}}`). The engine itself never makes the call: it returns the intent
  and the host delivers it, so the core stays network-free. Use it to ping a CRM, chat channel or
  automation tool when someone qualifies. Send the **minimum** payload, prefer signed `https://`
  endpoints, and don't forward free text or behavioral data you don't need (see
  [docs/DATA_SECURITY.md](../../docs/DATA_SECURITY.md)).
- **Attention checks:** write them as an ordinary question plus a `disqualify` rule on the wrong
  answer, with its own `reason_code` (e.g. `failed_attention`). `settings.anti_fraud` is declared in
  the schema and its references are validated, but the engine does **not** enforce it, so don't rely
  on it. `settings.retake` (`once`, the default, or `multiple`) **is** enforced.
- **Piping:** `{{q:qid.answer}}` (label) / `{{q:qid.value}}` (stored value) / `{{q:qid.row:rid.answer}}`
  (grid cell), `{{url:uid}}`, `{{var:name}}`, `{{ctx:fact}}` (alias `{{behavior:…}}`). Add a fallback
  after `|` whenever the value can be missing: `{{q:q_fav.answer|your favourite}}`.
  Option piping: `options_source: {pipe_from, mode: selected|not_selected|all}`. Exclusive options
  aren't piped forward, and a question left with no options is skipped. Pipe only from earlier pages
  (`validate` rejects same/later-page piping) and fix every `[warn]` about a blank token.
- **Repeating** ("ask the same thing for each brand they picked"): put
  `repeat: {source, mode?, by?, exclude?, max?, randomize?}` on a **page** (the whole page runs once
  per item) or a **question** (the question appears once per item on its page). `source` is a choice
  question on an earlier page; `mode: not_selected` repeats over the options *not* picked, and
  `by: value` + a number question gives N passes (items `1`..`N`). `max` defaults to 50 (hard cap 150);
  `randomize: true` shuffles the order per respondent. A question repeat can't sit on a repeated page.
  Inside the repeat, pipe `{{loop:label}}` / `{{loop:value}}` / `{{loop:index}}` / `{{loop:total}}`
  (index/total count only the passes actually shown) and test `{ref:{kind:loop, id:value}}`.
  Each item's answer is stored as `qid[item]` (e.g. `q_rate[netflix]`). Reference it that way in
  logic, tokens and quotas; `q_rate[@]` means "this item's answer" inside a later repeat over the same
  source, which is how you follow up only on the brands rated 5. Exclusive options ("none") are never
  repeated. Option values of the source must be token-safe (letters/digits/`_`/`-`). Mid-loop, a
  disqualify/overquota/complete ends the survey at once; a `skip` waits until the loop finishes.
  Markdown authoring: put `- @repeat: {"source": "q_brands"}` (JSON) directly under `## Page <id>`.
- **Behavioral enrichment** (the "pull behavior, then ask why" pattern): declare a fact in
  `settings.enrichments` (`event_type` + `window` + `criteria` + `operation`), gate a page on it
  (`{ref:{kind:enrichment, id:…}}`), and pipe it into a question title with `{{ctx:…}}` /
  `{{behavior:…}}`. Facts are resolved once, at session start, by the configured provider
  (`SURVEY_ENRICH`): `static` (fixtures, for demos and tests), `firebolt` (a reference warehouse
  adapter), or your own adapter via an import path (`your.pkg:Provider`). There is no built-in HTTP,
  database or geo-IP source; write an adapter for those. In the reference adapter, SQL identifiers are
  validated and values are parameterized.
- **`source: llm` facts are different:** they're analysed **at completion** and stored on the response
  (exported under `derived`). They can't drive live logic, piping or routing, and aren't shown
  mid-survey. They're opt-in (`SURVEY_LLM_ENRICH`) and disclosed in the consent text.

## Rules

- A question gated on another answer must be on a **later page** than that answer (the validator
  rejects same-page dependencies). For a per-item follow-up, that means a second repeated page.
- Every screen-out is a `disqualify` with a `reason_code`; every survey has a terminal `complete`.
- No dead branches; every `skip.to_page` exists; every enrichment used is declared.

## After editing

`survey-agent validate …` then `survey-agent simulate … --n 500` to prove the new paths are reachable
and terminate correctly. Then **regenerate the readable `.md` from the edited JSON** with
`survey-agent render <slug>.survey.json --write` (or re-run `save_survey` over MCP, which rewrites
both). Don't use `compile` to sync: it goes `.md` → `.json`, the wrong way after a JSON edit. Over MCP,
the `validate_survey`, `simulate_survey` and `save_survey` tools do the same jobs.
