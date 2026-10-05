# Capability matrix

What survey-agent supports today, what it supports in part, what it doesn't support yet, and what is
out of scope by design. Each row is checked against the engine, the runtime and the JSON Schema
(`src/survey_agent/engine/schema/survey.schema.json`), which is the authoritative list of what a
survey can express.

**Legend:** ✅ supported · ◑ partial · ⬜ not supported (yet) · ⊘ out of scope by design

Three things are out of scope on purpose, and they shape the rest of the table:

- **Sampling, targeting, panel-wide quotas and incentive payment.** survey-agent emits a drop-in
  `survey_url`; your panel, CRM or email system decides who gets it and pays them. Integrations hand
  results back to that system.
- **Custom scripting.** Logic is bounded data interpreted by the engine, never code. See
  [LOGIC_IS_DATA.md](LOGIC_IS_DATA.md).
- **Rich, niche capture widgets** (sliders, heatmaps, max-diff, conjoint…) are not in the core. They
  belong in frontend plugins.

---

## Question types

16 types (the schema's `type` enum), plus hidden values.

| Capability | Status | Notes |
|---|---|---|
| Single choice (`radio`) | ✅ | |
| Multiple choice (`checkbox`) | ✅ | per-option `exclusive: true` for "None of these" |
| Dropdown, single (`dropdown`) and multi (`dropdown_multi`) | ✅ | |
| Short text (`text`) / long text (`textarea`) | ✅ | |
| Number (`number`) | ✅ | `min` / `max` |
| Rating scale (`rating`) | ✅ | `scale_points` |
| Net Promoter Score (`nps`) | ✅ | |
| Ranking (`ranking`) | ✅ | ordered list with a reorder control; compare with `equals_ordered` |
| Grid / matrix (`grid`) | ✅ | `rows` × `options`; `cell_type: radio` (one per row) or `checkbox` (several per row); answer stored as `{row: choice(s)}` |
| Date (`date`) | ✅ | `YYYY-MM-DD`, with `min` / `max` bounds |
| File upload (`file`) | ✅ | checked for extension, size and count; bytes go to a pluggable blob store (`SURVEY_UPLOAD_STORE`: `local`, `s3`, or your own import path) and the answer keeps only a reference |
| Instructions (`instructions`) | ✅ | display-only; `body` renders a safe Markdown subset. `body` also works as a description under any question |
| Image stimulus (`image`) | ✅ | display-only; `media.url`, `alt`, `caption`, size controls |
| Video stimulus (`video`) | ✅ | display-only; YouTube links embed, other URLs play in a `<video>` element |
| Hidden values | ✅ | inbound URL variables are captured automatically; the `hidden` block stores computed fields (see [Calculations](#calculations-and-hidden-values)) |
| "Other, please specify" write-in on a choice | ⬜ | model it as a separate text question shown when "Other" is picked |
| Star rating / slider / slider list | ⬜ | frontend plugin territory |
| Image select, heatmap, signature, audio/video *response* | ⊘ | frontend + storage plugins |
| Max-diff, conjoint, card sort, constant sum, cascading dropdowns | ⊘ | specialist methods; plugins |

## Validation

| Capability | Status | Notes |
|---|---|---|
| Required | ✅ | per question; display-only types are never required |
| Number range | ✅ | `min` / `max`; `percent: true` for 0–100 |
| Text length | ✅ | `min_length` / `max_length` |
| Minimum words | ✅ | `min_words`, a model-free guard against one-word answers |
| Email | ✅ | `format: email` |
| Phone | ✅ | `format: phone` with `phone_region`: `us`, `uk` or `intl` |
| Regular expression | ✅ | `regex` + `regex_message`; `validate` rejects patterns over 200 characters or with nested unbounded quantifiers, and at most 2,000 characters of input are matched |
| Date bounds | ✅ | `min` / `max` on `date` |
| File constraints | ✅ | `extensions`, `max_files`, `max_size_kb` |
| Custom error text | ✅ | `format_message`, `regex_message` |

## Logic

One condition language for every surface: a leaf compares one reference with one value, a group joins
leaves with `and` / `or`, and groups nest. Details in [LOGIC_IS_DATA.md](LOGIC_IS_DATA.md).

| Capability | Status | Notes |
|---|---|---|
| Page display logic | ✅ | `display_logic` on a page |
| Question display logic | ✅ | `display_logic` on a question |
| Option display logic | ✅ | `display_logic` on an option |
| Grid-row display logic | ✅ | `display_logic` on a row |
| Skip to a later page | ✅ | `skip` action → `to_page` (forward only) |
| Screen-out | ✅ | `disqualify` action with `reason_code`, optional message and redirect |
| Over-quota exit | ✅ | `overquota` action |
| Conditional completion | ✅ | `complete` action with a `when` |
| Set a variable | ✅ | `set_variable` with a literal `value` or `value_from` a question |
| AND / OR groups, nested | ✅ | |
| Operators | ✅ | 16: equality (set-based and ordered), contains, one-of, answered / not answered, numeric comparisons, `between`, `regex_match`, `is_full` / `is_not_full` |
| Logic on URL variables, enrichment facts, quota state | ✅ | `ref.kind`: `question`, `url_var`, `variable`, `quota`, `enrichment`, `loop` |
| Language / device / location logic | ◑ | branch on a URL variable your link passes (e.g. `lang`), or on an enrichment fact your adapter supplies; nothing is detected automatically |
| Back navigation | ⊘ | flow is forward-only by design, so a respondent can't go back and change an answer that screened them out; a browser Back submit re-renders the current page |
| Custom scripting | ⊘ | logic is data; there is no script action |

## Piping

| Capability | Status | Notes |
|---|---|---|
| Answer piping | ✅ | `{{q:qid.answer}}` (label) or `{{q:qid.value}}` (stored value) |
| Grid-cell piping | ✅ | `{{q:qid.row:row_id.answer}}` |
| URL-variable piping | ✅ | `{{url:uid}}` |
| Variable / hidden-value piping | ✅ | `{{var:name}}` |
| Enrichment piping | ✅ | `{{ctx:fact}}` |
| Fallback text | ✅ | `{{q:q_fav.answer\|your favourite}}` renders the fallback when the value is blank |
| Option piping | ✅ | `options_source: {pipe_from, mode}` with `selected`, `not_selected` or `all`; exclusive options aren't carried forward, and a question left with no options is skipped |
| Piping checks | ✅ | `validate` rejects tokens that read the same or a later page and unknown kinds, fields or rows, and warns when a piped value can be missing; `simulate` reports tokens that rendered blank |
| Piping into URLs and payloads | ✅ | redirect URLs, webhook URLs and payloads resolve the same tokens |

## Repeating

| Capability | Status | Notes |
|---|---|---|
| Repeat a question per item | ✅ | `repeat` on a question; each item's answer is its own key, `q_rate[netflix]` |
| Repeat a page per item | ✅ | `repeat` on a page; `{{loop:index}}` / `{{loop:total}}` for "2 of 3" |
| Repeat over selected or unselected items | ✅ | `mode: selected` / `not_selected` |
| Repeat N times from a number | ✅ | `by: "value"`; `max` caps it (hard cap 150) |
| Exclude items | ✅ | exclusive options are never repeated; `exclude: [...]` for others |
| Randomized item order | ✅ | `randomize: true`, seeded per respondent; otherwise source-option order |
| Logic on repeated answers | ✅ | `q_rate[netflix]` anywhere, and `q_rate[@]` for "this item's answer" inside a later repeat |
| Export | ✅ | one column per item; each entry carries `repeat: {source, value, label}` |

## Randomization

| Capability | Status | Notes |
|---|---|---|
| Option order | ✅ | `shuffle`, `rotate`, `reverse`; seeded and stable per respondent |
| Anchored options | ✅ | `fixed: [...]` keeps "Other" / "None" in place |
| Question order within a page | ✅ | `randomization` on a page |
| Page order | ⬜ | not supported; the schema rejects a page-order setting rather than ignore it |

## Quotas

| Capability | Status | Notes |
|---|---|---|
| Overall cap | ✅ | `scope: survey` + `limit` |
| Cell quota | ✅ | `scope: answer` + `ref` + `match`; the `ref` can be a question, URL variable, variable (including a computed segment) or enrichment fact |
| Act when full | ✅ | `is_full` in an `overquota` (or any) action's `when` |
| Counting | ✅ | completes are counted per survey version in the configured store |
| Soft quota (warn but continue) | ⬜ | |
| Panel-wide or cross-survey quotas | ⊘ | belong to your panel or sampling system |

## Calculations and hidden values

| Capability | Status | Notes |
|---|---|---|
| Computed fields | ✅ | `hidden` block with a calc tree: `add`, `sub`, `mul`, `div`, `min`, `max`, `avg`, `sum`, `count_selected`, `score`, `bucket`, `concat`, `round` |
| Conditional static values | ✅ | `value` + `when` |
| Use in logic, piping, quotas | ✅ | computed values are variables |
| Stored on the response | ✅ | exported under `derived` |
| Time on survey | ◑ | start and submit times are recorded and the report shows length of interview; not available as a calc input |
| Random number | ⬜ | |

## Delivery and integrations

| Capability | Status | Notes |
|---|---|---|
| Drop-in survey link | ✅ | `/s/<id>?uid=…&rid=…`; every query parameter is captured into `url_variables` |
| Identity variables | ✅ | `settings.identity` names the respondent and response variables (default `uid` / `rid`) |
| Signed links | ✅ | `survey-agent sign-url`; the runtime verifies when `settings.url_signing.required` is set and `SURVEY_URL_SECRET` is configured |
| Retake control | ✅ | `settings.retake`: `once` (default; an in-progress link resumes, a finished one is locked) or `multiple` |
| Redirect on any terminal | ✅ | per-terminal `redirect`, or the `redirect` integration's per-status URLs with chosen URL variables appended |
| Lifecycle passback | ✅ | `SURVEY_INTEGRATION`: `none` (default), `redirect`, `webhook`, `panel`, `mailchimp`, or your own import path; neutral events `started` / `completed` / `terminated` / `terminated_by_user` with an idempotency key |
| Signed passbacks | ✅ | when a secret is set: `webhook` posts with `SURVEY_WEBHOOK_SECRET`, `panel` posts with `SURVEY_PANEL_SHARED_SECRET` |
| Mid-survey webhook | ✅ | `webhook` action, gated by `when`, fires once per session; optional durable outbox (`SURVEY_OUTBOX=1`) for these actions |
| Abandonment detection | ⬜ | the stdlib runtime never emits `terminated_by_user` on its own |
| Email notifications | ⬜ | no send-email integration; write one against the integration seam |
| Conversational delivery | ◑ | the same survey runs as a chat (`convo`, `/chat`, MCP `begin_survey`); grids, rankings and file uploads aren't supported in chat — use the form for those. See [AGENTIC_MODES.md](AGENTIC_MODES.md) |
| Themes | ✅ | built-in `dark`, `ocean`, `paper` over a shared base; your own via `SURVEY_THEMES_DIR`; per-survey `css` |
| Mobile layout | ✅ | responsive pages, fluid media, scrolling grids, matching mobile keyboards |
| Translations | ⬜ | no built-in multi-language surveys; pass `lang` and branch on it, or field one survey per language |

## Enrichment

| Capability | Status | Notes |
|---|---|---|
| Per-respondent facts from your data | ✅ | declared in `settings.enrichments`; usable in logic, piping (`{{ctx:…}}`) and routing |
| Providers | ◑ | `static` (fixtures for demos and tests) and `firebolt` (a reference warehouse adapter) ship; anything else, including HTTP APIs, other databases, geo-IP or user agent, needs an adapter you write (`SURVEY_ENRICH=your.pkg:Provider`) |
| Query safety | ✅ | in the reference adapter, SQL identifiers are validated and values are parameterized |
| LLM analysis of answers | ✅ | `source: llm`; opt-in (`SURVEY_LLM_ENRICH` + `opt_in: true`), audited, runs at completion and is stored on the response. It does not drive live logic or piping and is never shown to the respondent |

## Reporting and export

| Capability | Status | Notes |
|---|---|---|
| HTML report | ✅ | `survey-agent report`: funnel, incidence, length of interview, screen-out reasons, quota fill, per-question charts, computed fields |
| AI read-out | ✅ | `report --insights` / `insights`; offline stub without a key |
| Live dashboard | ✅ | `survey-agent dashboard --live` |
| Tabular export | ✅ | `survey-agent export --format csv\|parquet` |
| Canonical response record | ✅ | stable export shape: `id`, dates, `status`, `url_variables`, `survey_data`, `derived` |
| Cross-tabs | ◑ | not built in; export and cross-tab in your own tools |
| Backup / restore | ✅ | `survey-agent backup` / `restore` |

## Lifecycle and operations

| Capability | Status | Notes |
|---|---|---|
| Validate | ✅ | schema + referential checks with plain-language messages |
| Simulate | ✅ | synthetic respondents through the real engine; coverage gate on completion and every screen-out |
| Persona pre-test | ✅ | `pretest`, optionally against a declared audience. See [PERSONAS.md](PERSONAS.md) |
| Methodology critique | ✅ | `critique`; advisory |
| Markdown authoring | ✅ | `compile` turns a `.md` survey into JSON |
| Versioning | ◑ | each survey carries a `version`, and quota counts are kept per version. `publish` to the `mongo` definition source bumps the version on any content change; with the default `file` source you bump `version` yourself |
| Templates | ✅ | `templates`, `new`, `copy`, `templatize` |
| Serving gate | ✅ | `serve --public` serves only `live` surveys (`SURVEY_SERVE_STATES` to change); others show a "not open yet" page |
| Pause / close | ✅ | `survey-agent status <id> --set paused\|closed`; `--set live` resumes |
| Launch approval | ✅ | with `SURVEY_LAUNCH_APPROVAL_TOKEN` set, nothing marks a survey live without the token; the MCP connector always requires it |
| Deploy hook | ✅ | `launch --deploy` runs your configured `deploy.command` |
| Storage backends | ✅ | `SURVEY_STORE`: `file` (default), `memory`, `sqlite`, `mongo`, `http`, or your own |
| Definition export / import / diff | ✅ | `defs-export`, `defs-import`, `defs-diff` |
| Install self-check | ✅ | `survey-agent doctor` |

## Security and data protection

| Capability | Status | Notes |
|---|---|---|
| No code in survey definitions | ✅ | the engine interprets a closed logic language; there is no `eval` |
| Data-subject requests | ✅ | `survey-agent dsar <uid>` finds a respondent's responses and, with `--delete`, erases them from the configured store; backups and downstream copies are yours to handle |
| Rate limiting | ◑ | opt-in, per instance (`SURVEY_RATE_LIMIT`); use a load balancer or WAF for a global cap |
| Client metadata capture | ◑ | opt-in (`SURVEY_CAPTURE_CLIENT_META`): IP, user agent and a browser fingerprint stored on the response for your own fraud review |
| Anti-fraud settings | ◑ | `settings.anti_fraud` (`attention_checks`, `min_page_seconds`, `block_duplicate_uid`) is declared but not enforced by the engine yet; write an attention check as an ordinary screen-out rule |
| API authentication | ◑ | the API gateway runs **open unless you configure auth** (bearer token, Google OAuth or `sa_` API keys); the operator console binds to localhost and has no auth |
| Encryption at rest, redaction, tenant scoping, compliance | ⊘ | deployer responsibilities; see [DATA_SECURITY.md](DATA_SECURITY.md) and [OPERATOR_RESPONSIBILITIES.md](OPERATOR_RESPONSIBILITIES.md) |
